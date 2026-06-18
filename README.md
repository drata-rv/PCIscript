# PCIscript

Creates Drata evidence library placeholders from an artifact list CSV. Names each item with a BU prefix (`[GLOBAL]`, `[MINDBODY]`, `[CLASSPASS]`, `[BOOKER]`) and links it to the relevant DCF controls at creation time.

## Requirements

Python 3.11+

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Dry run (default — no writes)
python3.11 build_evidence_placeholders.py --input path/to/artifact_list.csv

# Live run
python3.11 build_evidence_placeholders.py --input path/to/artifact_list.csv --live

# Route unknown BU suffixes (A, FMX, MAI) to a workspace instead of skipping
python3.11 build_evidence_placeholders.py --input path/to/artifact_list.csv --live \
  --map-suffix MAI=mindbody \
  --map-suffix FMX=booker
```

## Flags

| Flag | Required | Description |
|------|----------|-------------|
| `--input` | Yes | Path to Baker Tilly artifact list CSV |
| `--live` | No | Execute writes. Omit to dry-run |
| `--map-suffix SUFFIX=WORKSPACE` | No | Route an unrecognized BU suffix to a workspace role (`main`, `mindbody`, `classpass`, `booker`). Repeatable. Default is skip |

## API Key

Set via environment variable (preferred):

```bash
export DRATA_API_KEY=your_key_here
```

Or the script will prompt at startup.

## Startup

The script walks you through workspace role assignment interactively. You'll need the numeric IDs for:

- Main PCI workspace (Baker Tilly audit hub)
- Mindbody BU workspace
- Classpass BU workspace
- Booker BU workspace

## Output

| File | Contents |
|------|----------|
| `run_log_{timestamp}.csv` | Every item processed — action, workspace, name, HTTP status |
| `unrouted_{timestamp}.csv` | Rows skipped due to unrecognized BU suffix |
