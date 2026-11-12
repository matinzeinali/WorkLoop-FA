"""Structured results the agents hand to each other.

The same models are the argument schemas of the terminal tools
(assign_task, submit_verdict, finish_task), so the LLM sees exactly these fields.
This module imports nothing from the app, so the tool registry can use it too.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Task(_Model):
    """assign_task arguments: what the Boss sends to the Worker."""

    goal: Text = Field(description="What must be done, stated clearly and completely.")
    files: list[Text] = Field(
        min_length=1, description="Files to create or change, relative to the project root."
    )
    acceptance_criteria: list[Text] = Field(
        min_length=1, description="Checks you will use to decide whether the work is correct."
    )
    notes: str = Field("", description="Extra context: constraints, style, hints. Optional.")


class Bug(_Model):
    location: Text = Field(description="Where it happens: file and line, function, endpoint or command.")
    description: Text = Field(description="What is wrong (expected vs actual).")
    reproduce: Text = Field(description="Exact steps or command that show the bug.")


class Verdict(_Model):
    """submit_verdict arguments: the Boss's decision for a round."""

    status: Literal["approved", "rejected"] = Field(
        description="approved: all acceptance criteria pass. rejected: something is wrong."
    )
    summary: Text = Field(description="Short summary of what you tested and the result.")
    bugs: list[Bug] = Field(default_factory=list, description="Required when rejected: every bug found.")
    fix_instructions: list[Text] = Field(
        default_factory=list, description="Required when rejected: concrete steps to fix each bug."
    )

    @model_validator(mode="after")
    def _rejected_needs_details(self) -> "Verdict":
        if self.status == "rejected" and (not self.bugs or not self.fix_instructions):
            raise ValueError(
                "A rejected verdict needs at least one bug and at least one fix instruction."
            )
        return self


class WorkerReport(_Model):
    """finish_task arguments: what the Worker reports back."""

    summary: Text = Field(description="What you did, in a few sentences.")
    changed_files: list[Text] = Field(
        default_factory=list, description="Every file you created, changed or deleted."
    )
    notes: str = Field("", description="Choices you made, open questions, anything the Boss should know.")


class FileChanges(_Model):
    """Files the user changed by hand after a failure (relative paths)."""

    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
