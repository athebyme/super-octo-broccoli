# -*- coding: utf-8 -*-
"""Stable contract shared by the chat runtime and the Image Lab backend."""

CHAT_IMAGE_BACKEND = "openrouter"
CHAT_IMAGE_MODEL = "google/gemini-3.1-flash-lite-image"
CHAT_IMAGE_STRATEGY = "native_scene"
CHAT_IMAGE_COMPOSITION_MODE = "single"
# OpenRouter's dedicated image API bills the exact request in USD.  This is a
# conservative preflight/display estimate for a 1K 3:4 edit with one reference,
# not a provider promise; the response usage remains the source of truth.
CHAT_IMAGE_RESOLUTION = "1K"
CHAT_IMAGE_COST_USD = 0.04
CHAT_IMAGE_COST_RUB = 3.30

# The prompt writer is deliberately separate from the seller's primary chat
# model.  Both prompt writing and the final image request go through OpenRouter
# so the seller flow has one provider boundary and one proxy policy.
CHAT_IMAGE_PROMPT_PROVIDER = "openrouter"
CHAT_IMAGE_PROMPT_MODEL = "google/gemini-2.5-flash"
CHAT_IMAGE_PROMPT_MAX_TOKENS = 420

CHAT_IMAGE_WAIT_SECONDS = 210
CHAT_IMAGE_POLL_SECONDS = 2.0

CHAT_IMAGE_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
CHAT_IMAGE_ACTIVE_STATUSES = frozenset({
    "queued", "running", "remote_running", "finalizing",
})


def chat_image_cost_label() -> str:
    """Human-readable estimate used by approval plans."""
    return "≈" + f"{CHAT_IMAGE_COST_RUB:.2f}".replace(".", ",") + " ₽"
