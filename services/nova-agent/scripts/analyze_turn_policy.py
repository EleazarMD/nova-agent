#!/usr/bin/env python3
"""
Analyze Nova turn policy observations.

Outputs frequency stats, shadow policy disagreements, and similarity clusters
using the embedded observations in the local SQLite DB.
"""

import argparse
import json
import math
import os
import sqlite3
import sys

DB_PATH = os.environ.get("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "nova.db"))

def cosine_similarity(v1, v2):
    dot_product = sum(a * b for a, b in zip(v1, v2))
    magnitude1 = math.sqrt(sum(a * a for a in v1))
    magnitude2 = math.sqrt(sum(b * b for b in v2))
    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0
    return dot_product / (magnitude1 * magnitude2)

def fetch_view(cursor, view_name):
    cursor.execute(f"SELECT * FROM {view_name}")
    columns = [description[0] for description in cursor.description]
    rows = cursor.fetchall()
    return columns, rows

def print_table(title, columns, rows):
    print(f"\n=== {title} ===")
    if not rows:
        print("No data.")
        return
    
    col_widths = [max(len(str(col)), max(len(str(row[i])) for row in rows)) for i, col in enumerate(columns)]
    
    header = " | ".join(str(col).ljust(width) for col, width in zip(columns, col_widths))
    print(header)
    print("-" * len(header))
    
    for row in rows:
        print(" | ".join(str(val).ljust(width) for val, width in zip(row, col_widths)))

def analyze_clusters(cursor):
    cursor.execute("""
        SELECT o.id, o.normalized_text, e.embedding_json
        FROM turn_policy_observations o
        JOIN turn_policy_embeddings e ON o.id = e.observation_id
    """)
    rows = cursor.fetchall()
    
    if len(rows) < 2:
        print("\n=== Semantic Similarity Pairs ===")
        print("Not enough embeddings to compare.")
        return

    embeddings = []
    for row in rows:
        try:
            vec = json.loads(row[2])
            embeddings.append({"id": row[0], "text": row[1], "vec": vec})
        except Exception:
            pass

    pairs = []
    for i in range(len(embeddings)):
        for j in range(i + 1, len(embeddings)):
            sim = cosine_similarity(embeddings[i]["vec"], embeddings[j]["vec"])
            pairs.append((sim, embeddings[i]["text"], embeddings[j]["text"]))
    
    pairs.sort(key=lambda x: x[0], reverse=True)
    
    print("\n=== Top Semantic Similarity Pairs ===")
    for sim, text1, text2 in pairs[:10]:
        if sim > 0.8:
            print(f"[{sim:.3f}] '{text1}'  <==>  '{text2}'")
        else:
            print(f"[{sim:.3f}] '{text1}'  <==>  '{text2}' (Low similarity)")
            break

def main():
    parser = argparse.ArgumentParser(description="Analyze Nova turn policy observations.")
    parser.add_argument("--db", default=DB_PATH, help="Path to nova.db")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Database not found at {args.db}")
        sys.exit(1)

    with sqlite3.connect(args.db) as conn:
        cursor = conn.cursor()
        
        # Intent Frequency
        cols, rows = fetch_view(cursor, "policy_intent_frequency")
        print_table("Intent Frequency", cols, rows)
        
        # Outcome Frequency
        cols, rows = fetch_view(cursor, "policy_outcome_frequency")
        print_table("Outcome Frequency", cols, rows)
        
        # Shadow Disagreements
        cols, rows = fetch_view(cursor, "policy_shadow_disagreements")
        print_table("Shadow Disagreements", cols, rows)
        
        # Clusters
        analyze_clusters(cursor)

if __name__ == "__main__":
    main()
