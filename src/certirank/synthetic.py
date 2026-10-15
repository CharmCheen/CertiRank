"""Seeded mechanism fixtures, explicitly separate from real LLM evidence."""

from itertools import combinations
import random

from certirank.oracle import TableSource


def make_table(n, responses, seed):
    rng = random.Random(seed)
    rates = (0.0, 0.05, 0.15, 0.35, 0.5)
    return {pair: [rng.random() >= rates[(pair[0] * 31 + pair[1]) % len(rates)] for _ in range(responses)]
            for pair in combinations(range(n), 2)}


def make_cases(config):
    cases = []
    for n in config["diagnostics"]["synthetic_sizes"]:
        for split, count in (("dev", 3), ("heldout", 2)):
            for index in range(count):
                seed = config["diagnostics"]["synthetic_seed"] + n * 100 + index + (10000 if split == "heldout" else 0)
                q = f"synthetic-N{n}-{split}-{index}"
                cases.append({"query_id": q, "query_group": q, "split": split, "source_kind": "synthetic",
                    "label_source": "synthetic latent strict order; relevant candidates 0..K-1",
                    "source": TableSource(make_table(n, config["formal"]["response_count"], seed), context_id=q),
                    "n": n, "k": config["k"], "relevant": set(range(config["k"]))})
    return cases
