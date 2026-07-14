#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create the seller-scoped image generation experiment journal."""

import os
import sqlite3
import sys


def migrate(db_path: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS image_generation_experiments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL,
                imported_product_id INTEGER,
                backend VARCHAR(32) NOT NULL,
                model VARCHAR(120) NOT NULL,
                scene_key VARCHAR(32),
                composition_mode VARCHAR(32) NOT NULL DEFAULT 'single',
                source_photo_indices_json TEXT NOT NULL DEFAULT '[0]',
                prompt TEXT NOT NULL,
                prompt_sha256 VARCHAR(64) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'queued',
                remote_job_id VARCHAR(120),
                error TEXT,
                latency_s FLOAT,
                estimated_cost_rub FLOAT NOT NULL DEFAULT 0.0,
                source_path VARCHAR(500),
                background_path VARCHAR(500),
                final_path VARCHAR(500),
                quality_json TEXT,
                composite_metadata_json TEXT,
                rating INTEGER,
                rating_tags_json TEXT,
                rating_comment TEXT,
                created_at DATETIME NOT NULL,
                started_at DATETIME,
                completed_at DATETIME,
                rated_at DATETIME,
                FOREIGN KEY(seller_id) REFERENCES sellers(id) ON DELETE CASCADE,
                FOREIGN KEY(imported_product_id) REFERENCES imported_products(id) ON DELETE SET NULL
            )
        """)
        columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(image_generation_experiments)"
            ).fetchall()
        }
        if "source_path" not in columns:
            connection.execute(
                "ALTER TABLE image_generation_experiments ADD COLUMN source_path VARCHAR(500)"
            )
        if "composition_mode" not in columns:
            connection.execute(
                "ALTER TABLE image_generation_experiments "
                "ADD COLUMN composition_mode VARCHAR(32) NOT NULL DEFAULT 'single'"
            )
        if "source_photo_indices_json" not in columns:
            connection.execute(
                "ALTER TABLE image_generation_experiments "
                "ADD COLUMN source_photo_indices_json TEXT NOT NULL DEFAULT '[0]'"
            )
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_image_generation_experiments_seller_id "
            "ON image_generation_experiments(seller_id)",
            "CREATE INDEX IF NOT EXISTS ix_image_generation_experiments_imported_product_id "
            "ON image_generation_experiments(imported_product_id)",
            "CREATE INDEX IF NOT EXISTS ix_image_generation_experiments_status "
            "ON image_generation_experiments(status)",
            "CREATE INDEX IF NOT EXISTS ix_image_generation_experiments_remote_job_id "
            "ON image_generation_experiments(remote_job_id)",
            "CREATE INDEX IF NOT EXISTS ix_image_generation_experiments_prompt_sha256 "
            "ON image_generation_experiments(prompt_sha256)",
            "CREATE INDEX IF NOT EXISTS idx_image_exp_seller_created "
            "ON image_generation_experiments(seller_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_image_exp_seller_status "
            "ON image_generation_experiments(seller_id, status)",
        ):
            connection.execute(statement)
        connection.commit()
        print("image_generation_experiments: OK")
    finally:
        connection.close()


if __name__ == "__main__":
    path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("DATABASE_PATH", "data/seller_platform.db")
    )
    migrate(path)
