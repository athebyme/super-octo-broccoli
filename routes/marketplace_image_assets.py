"""Public, immutable marketplace image delivery.

The route accepts only an HMAC-signed content digest already materialized on
disk.  It never accepts or fetches a remote URL.
"""

from __future__ import annotations

from flask import abort, request, send_file

from services.marketplace_image_assets import (
    MarketplaceImageAssetError,
    resolve_public_asset,
)


def register_marketplace_image_asset_routes(app) -> None:
    @app.get(
        "/marketplace-assets/images/<digest>.jpg",
        endpoint="marketplace_image_asset",
    )
    def marketplace_image_asset(digest: str):
        try:
            path = resolve_public_asset(
                digest=digest,
                signature=request.args.get("sig", ""),
                config=app.config,
                secret_key=app.config["SECRET_KEY"],
            )
        except MarketplaceImageAssetError as exc:
            if exc.code == "media_asset_signature_invalid":
                abort(403)
            abort(404)
        response = send_file(
            path,
            mimetype="image/jpeg",
            conditional=True,
            etag=True,
            max_age=31_536_000,
        )
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
