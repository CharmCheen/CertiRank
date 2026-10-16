# Temperature Calibration Results

- Completed: 160 (A null) + 160 (B zero) = 320 records
- Invalid: A 0 / B 0

| Line | pairs | ever-flip | ever-flip ratio | mean pairwise disagree | disagree pairs |
|---|---:|---:|---:|---:|---:|
| A (temperature omitted) | 32 | 3 | 0.094 | 0.0437 | 3 |
| B (temperature=0.0) | 32 | 6 | 0.188 | 0.0813 | 6 |

## Cross-line

- Common pairs: 32
- Majority verdict differs: 1 (0.031)
- Aligned rep differs: 8/160 (0.050)

## Flipped pairs

### A
- PLAIN-142 `PLAIN-142|MED-3609|MED-2940` T/F=4/1 seq=[0, 1, 1, 1, 1]
- PLAIN-142 `PLAIN-142|MED-3609|MED-3619` T/F=3/2 seq=[1, 0, 0, 1, 1]
- PLAIN-142 `PLAIN-142|MED-3609|MED-3620` T/F=4/1 seq=[1, 0, 1, 1, 1]

### B
- PLAIN-11 `PLAIN-11|MED-2121|MED-2126` T/F=1/4 seq=[1, 0, 0, 0, 0]
- PLAIN-142 `PLAIN-142|MED-3609|MED-2940` T/F=2/3 seq=[0, 0, 1, 0, 1]
- PLAIN-142 `PLAIN-142|MED-3609|MED-3608` T/F=1/4 seq=[0, 0, 0, 1, 0]
- PLAIN-142 `PLAIN-142|MED-3609|MED-3620` T/F=4/1 seq=[1, 0, 1, 1, 1]
- PLAIN-142 `PLAIN-142|MED-3618|MED-2940` T/F=1/4 seq=[0, 1, 0, 0, 0]
- PLAIN-2429 `PLAIN-2429|MED-2292|MED-977` T/F=4/1 seq=[1, 0, 1, 1, 1]

## Interpretation

- Observable run-to-run disagreement exists under **both** temperature settings in this sample.
- 5 repeats only support exploratory claims.
- Proceeding to formal Dev is scientifically justified on non-determinism grounds; quality/value of ReviewValue remains unproven.
