#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Turn raw GPU backgrounds into audited foreground composites locally."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.infographic_quality import (  # noqa: E402
    canonicalize_image,
    compose_identity_preserving,
    evaluate_background_text,
    evaluate_final_image,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--gpu-out", required=True)
    parser.add_argument("--results", default="results.jsonl")
    parser.add_argument("--final-results", default="results_final.jsonl")
    args = parser.parse_args()

    bundle = Path(args.bundle)
    out = Path(args.gpu_out)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    products = {item["id"]: item for item in manifest.get("products", [])}
    rows = [
        json.loads(line)
        for line in (out / args.results).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    final_path = out / args.final_results
    with final_path.open("w", encoding="utf-8") as sink:
        for row in rows:
            final = dict(row)
            if row.get("status") != "background_only" or not row.get("artifact"):
                sink.write(json.dumps(final, ensure_ascii=False) + "\n")
                continue
            try:
                product = products[row["product_id"]]
                source = (bundle / product["photo"]).read_bytes()
                background = canonicalize_image((out / row["artifact"]).read_bytes())
                composite = compose_identity_preserving(background, source)
                check = evaluate_background_text(background)
                quality = evaluate_final_image(
                    composite.image_bytes,
                    identity_mode="pixel_preserved_composite",
                    text_mode="none",
                    claims_pass=True,
                    composite_metadata=composite.metadata,
                    background_text_check=check,
                )
                name = Path(row["artifact"]).stem.replace("_background", "_final") + ".png"
                (out / name).write_bytes(composite.image_bytes)
                final.update({
                    "status": quality["status"],
                    "output": name,
                    "quality": quality,
                    "composite_metadata": composite.metadata,
                    "error": "",
                })
            except Exception as exc:  # noqa: BLE001
                final.update({"status": "rejected", "output": None, "error": str(exc)[:300]})
            sink.write(json.dumps(final, ensure_ascii=False) + "\n")
    print(f"Final results: {final_path}")


if __name__ == "__main__":
    main()
