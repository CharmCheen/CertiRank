# Gate 3 NFCorpus Query Group Freeze

- Dataset: BEIR/nfcorpus (efe5be03f8c5b86a…)
- Retriever: BM25 k1=1.2 b=0.75, tokenizer lowercase [a-z0-9]+, title+text, Top-8
- Dev source: qrels/dev.tsv → selected 6/324
- Heldout source: qrels/test.tsv → selected 12/323
- Selection order: lexicographic query_id among pre-registered eligible
- Eligibility: ≥2 labeled in top8, ≥1 grade-2 in top8, BM25 nDCG@3 in (0.2, 0.95)

## Dev queries

| query_id | n_lab | n_perfect | nDCG@3 | query |
|---|---:|---:|---:|---|
| PLAIN-101 | 4 | 3 | 0.531 | How to Treat Multiple Sclerosis With Diet |
| PLAIN-11 | 2 | 2 | 0.387 | Living Longer by Reducing Leucine Intake |
| PLAIN-142 | 6 | 1 | 0.521 | Dealing With Air Travel Radiation Exposure |
| PLAIN-2429 | 2 | 2 | 0.387 | Diverticulosis: When Our Most Common Gut Disorder Hardly Exi |
| PLAIN-2479 | 3 | 3 | 0.235 | Stool pH and Colon Cancer |
| PLAIN-2539 | 4 | 1 | 0.840 | Cooked Beans or Sprouted Beans? |

## Heldout queries

| query_id | n_lab | n_perfect | nDCG@3 | query |
|---|---:|---:|---:|---|
| PLAIN-112 | 2 | 2 | 0.920 | Food Dyes and ADHD |
| PLAIN-165 | 4 | 1 | 0.202 | Breast Cancer & Alcohol: How Much is Safe? |
| PLAIN-175 | 3 | 1 | 0.882 | Diet and Cellulite |
| PLAIN-2 | 6 | 5 | 0.883 | Do Cholesterol Statin Drugs Cause Breast Cancer? |
| PLAIN-23 | 4 | 1 | 0.521 | How to Reduce Exposure to Alkylphenols Through Your Diet |
| PLAIN-2450 | 2 | 2 | 0.613 | Are Organic Foods Safer? |
| PLAIN-2490 | 3 | 3 | 0.765 | The Actual Benefit of Diet vs. Drugs |
| PLAIN-2540 | 2 | 1 | 0.760 | Does Cholesterol Size Matter? |
| PLAIN-2630 | 3 | 3 | 0.765 | Alkylphenol Endocrine Disruptors and Allergies |
| PLAIN-2640 | 7 | 2 | 0.832 | Chicken Salmonella Thanks to Meat Industry Lawsuit |
| PLAIN-2650 | 3 | 3 | 0.704 | Turmeric Curcumin and Osteoarthritis |
| PLAIN-2680 | 2 | 2 | 0.613 | Counteracting the Effects of Dioxins Through Diet |

## Boundaries

- Unlabeled docs in candidates are evaluation-as-zero (BEIR convention), not human-confirmed negatives.
- Heldout is frozen for static build/verify only; no LLM on heldout in this gate.
- No selection based on LLM flip behavior.
