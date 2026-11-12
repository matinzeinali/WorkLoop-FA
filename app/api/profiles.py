"""API profile endpoints and the provider list."""

from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.crypto import CryptoError, decrypt_api_key
from app.core.errors import ApiError, ErrorCode
from app.db import repository as repo
from app.db.database import get_db
from app.db.models import ApiProfile
from app.llm.client import (
    TestResult,
    get_provider,
    get_supported_providers,
    test_connection,
)

router = APIRouter(tags=["profiles"])

DbSession = Annotated[Session, Depends(get_db)]

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
ModelName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ApiKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ProviderId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


# --- Schemas ----------------------------------------------------------------


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class ConnectionFields(BaseModel):
    provider: ProviderId
    api_key: ApiKey
    model_name: ModelName
    base_url: str | None = None

    _clean_base_url = field_validator("base_url", mode="before")(_blank_to_none)


class ProfileCreate(ConnectionFields):
    name: Name


class ProfileTestRequest(ConnectionFields):
    pass


class ProfileUpdate(BaseModel):
    name: Name | None = None
    provider: ProviderId | None = None
    api_key: str | None = None  # missing or empty keeps the stored key
    model_name: ModelName | None = None
    base_url: str | None = None

    _clean_base_url = field_validator("base_url", mode="before")(_blank_to_none)

    @field_validator("api_key")
    @classmethod
    def _strip_key(cls, value: str | None) -> str | None:
        return value.strip() if value else None


class ProviderOut(BaseModel):
    id: str
    name: str
    requires_base_url: bool
    default_base_url: str | None
    example_models: list[str]


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    provider: str
    model_name: str
    base_url: str | None
    key_preview: str
    last_test_ok: bool | None
    last_test_message: str | None
    last_tested_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TestResultOut(BaseModel):
    ok: bool
    message: str
    latency_ms: int
    model: str


# --- Validation helpers -------------------------------------------------------


def _validate_provider(provider: str) -> None:
    if get_provider(provider) is None:
        raise ApiError.validation("provider", f"Unknown provider '{provider}'.")


def _validate_base_url(provider: str, base_url: str | None) -> None:
    info = get_provider(provider)
    if base_url is None:
        if info is not None and info.requires_base_url:
            raise ApiError.validation("base_url", f"base_url is required for provider '{provider}'.")
        return
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ApiError.validation("base_url", "base_url must be a valid http or https URL.")


def _validate_connection(provider: str, base_url: str | None) -> None:
    _validate_provider(provider)
    _validate_base_url(provider, base_url)


def _ensure_name_free(db: Session, name: str, exclude_id: int | None = None) -> None:
    if repo.get_profile_by_name(db, name, exclude_id=exclude_id) is not None:
        raise ApiError(
            ErrorCode.NAME_TAKEN, f"A profile named '{name}' already exists.", 409, {"name": name}
        )


def _get_or_404(db: Session, profile_id: int) -> ApiProfile:
    profile = repo.get_profile(db, profile_id)
    if profile is None:
        raise ApiError.not_found("Profile", profile_id)
    return profile


def _name_taken_from_integrity(name: str) -> ApiError:
    # Safety net for a race between the name check and the insert.
    return ApiError(ErrorCode.NAME_TAKEN, f"A profile named '{name}' already exists.", 409, {"name": name})


# --- Routes -----------------------------------------------------------------


@router.get("/providers", response_model=list[ProviderOut])
def list_providers() -> list[dict[str, Any]]:
    return get_supported_providers()


@router.get("/profiles", response_model=list[ProfileOut])
def list_profiles(db: DbSession) -> list[ApiProfile]:
    return list(repo.list_profiles(db))


@router.post("/profiles", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
def create_profile(body: ProfileCreate, db: DbSession) -> ApiProfile:
    _validate_connection(body.provider, body.base_url)
    _ensure_name_free(db, body.name)
    try:
        return repo.create_profile(
            db,
            name=body.name,
            provider=body.provider,
            api_key=body.api_key,
            model_name=body.model_name,
            base_url=body.base_url,
        )
    except IntegrityError as exc:
        db.rollback()
        raise _name_taken_from_integrity(body.name) from exc


@router.post("/profiles/test", response_model=TestResultOut)
async def test_unsaved_profile(body: ProfileTestRequest) -> dict[str, Any]:
    _validate_connection(body.provider, body.base_url)
    result = await test_connection(body.provider, body.api_key, body.model_name, body.base_url)
    return result.to_dict()


@router.put("/profiles/{profile_id}", response_model=ProfileOut)
def update_profile(profile_id: int, body: ProfileUpdate, db: DbSession) -> ApiProfile:
    profile = _get_or_404(db, profile_id)
    changes = body.model_dump(exclude_unset=True)

    # Fields sent as null mean "no change", except base_url where null clears it.
    for field in ("name", "provider", "model_name", "api_key"):
        if changes.get(field) is None:
            changes.pop(field, None)

    provider = changes.get("provider", profile.provider)
    base_url = changes["base_url"] if "base_url" in changes else profile.base_url
    _validate_connection(provider, base_url)

    if "name" in changes:
        _ensure_name_free(db, changes["name"], exclude_id=profile.id)

    try:
        return repo.update_profile(db, profile, changes)
    except IntegrityError as exc:
        db.rollback()
        raise _name_taken_from_integrity(changes.get("name", profile.name)) from exc


@router.delete("/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile(profile_id: int, db: DbSession) -> Response:
    profile = _get_or_404(db, profile_id)
    if repo.is_profile_in_use(db, profile.id):
        raise ApiError(
            ErrorCode.PROFILE_IN_USE,
            "This profile is assigned as Boss or Worker. Unassign it before deleting.",
            409,
            {"id": profile.id},
        )
    repo.delete_profile(db, profile)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/profiles/{profile_id}/test", response_model=TestResultOut)
async def test_saved_profile(profile_id: int, db: DbSession) -> dict[str, Any]:
    profile = _get_or_404(db, profile_id)
    try:
        api_key = decrypt_api_key(profile.api_key_encrypted)
    except CryptoError:
        result = TestResult(
            False, "Stored API key could not be decrypted. Enter the key again.", 0, profile.model_name
        )
    else:
        result = await test_connection(
            profile.provider, api_key, profile.model_name, profile.base_url
        )
    repo.save_test_result(db, profile, result.ok, result.message)
    return result.to_dict()
