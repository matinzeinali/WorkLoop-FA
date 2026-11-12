"""WorkLoop backend application package."""

import os

# LiteLLM downloads its model price list at import time unless told to use the bundled
# copy. Set it here, before any app module imports litellm (startup stays offline-safe).
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
