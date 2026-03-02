# Project Types

All project types follow [AEA Data Editor guidelines](https://aeadataeditor.github.io/aea-de-guidance/preparing-for-data-deposit) for reproducible research.

## Data Projects (`data_*`)

For data products and processing pipelines. Supports Python, R, and Stata.

**Python:**
```
data_hospital_project/
├── README.md                 # Project documentation
├── metadata.json             # Project metadata
├── requirements.txt          # Python dependencies
├── data/
│   ├── raw/                  # Original source data (DVC tracked)
│   ├── intermediate/         # Temporary processing results (DVC tracked)
│   └── final/                # Final processed data (DVC tracked)
├── schemas/
│   └── v1/
│       └── schema.json       # Data schema
├── code/
│   ├── _mintd_utils.py       # Utilities (paths, schema generation)
│   ├── ingest.py             # Data acquisition
│   ├── clean.py              # Data cleaning
│   └── validate.py           # Data validation
├── .gitignore
├── .dvcignore
├── dvc_vars.yaml             # DVC variables
└── dvc.yaml                  # Pipeline configuration
```

**R:**
```
data_hospital_project/
├── README.md
├── metadata.json
├── DESCRIPTION               # R package description
├── renv.lock                 # R environment snapshot
├── data/
│   ├── raw/
│   ├── intermediate/
│   └── final/
├── schemas/
│   └── v1/
│       └── schema.json
├── code/
│   ├── _mintd_utils.R
│   ├── ingest.R
│   ├── clean.R
│   └── validate.R
├── .gitignore
├── .dvcignore
├── dvc_vars.yaml
└── dvc.yaml
```

**Stata:**
```
data_hospital_project/
├── README.md
├── metadata.json
├── data/
│   ├── raw/
│   ├── intermediate/
│   └── final/
├── schemas/
│   └── v1/
│       └── schema.json
├── code/
│   ├── _mintd_utils.do
│   ├── ingest.do
│   ├── clean.do
│   └── validate.do
├── .gitignore
├── .dvcignore
├── dvc_vars.yaml
└── dvc.yaml
```

## Research Projects (`prj_*`)

For analysis and research projects with full AEA compliance.

**Python:**
```
prj_cost_study/
├── README.md                 # AEA-compliant documentation
├── metadata.json             # Project metadata
├── citations.md              # Data and software citations
├── requirements.txt          # Python dependencies
├── run_all.py                # Master run script
├── data/
│   ├── raw/                  # Original source data
│   └── analysis/             # Processed data for analysis
├── code/
│   ├── config.py             # Configuration (paths, seeds, lookups)
│   ├── _mintd_utils.py       # Utilities
│   ├── 01_data_prep/         # Data preparation scripts
│   ├── 02_analysis/          # Main analysis scripts
│   │   └── __init__.py
│   ├── 03_tables/            # Table generation
│   └── 04_figures/           # Figure generation
├── results/
│   ├── figures/              # Generated plots
│   ├── tables/               # Generated tables
│   ├── estimates/            # Model outputs
│   └── presentations/        # Presentation materials
├── notebooks/                # Jupyter notebooks
├── docs/                     # Documentation
├── references/               # Reference materials
├── tests/                    # Test files
├── .gitignore
└── .dvcignore
```

**R:**
```
prj_cost_study/
├── README.md
├── metadata.json
├── citations.md
├── DESCRIPTION
├── renv.lock
├── run_all.R                 # Master run script
├── .Rprofile
├── data/
│   ├── raw/
│   └── analysis/
├── code/
│   ├── config.R              # Configuration (paths, seeds, lookups)
│   ├── _mintd_utils.R
│   ├── 01_data_prep/
│   ├── 02_analysis/
│   │   └── analysis.R
│   ├── 03_tables/
│   └── 04_figures/
├── results/
│   ├── figures/
│   ├── tables/
│   ├── estimates/
│   └── presentations/
├── notebooks/
├── docs/
├── references/
├── tests/
├── .gitignore
└── .dvcignore
```

**Stata:**
```
prj_cost_study/
├── README.md
├── metadata.json
├── citations.md
├── run_all.do                # Master run script
├── data/
│   ├── raw/
│   └── analysis/
├── code/
│   ├── config.do             # Configuration (paths, seeds, lookups)
│   ├── _mintd_utils.do
│   ├── 01_data_prep/
│   ├── 02_analysis/
│   ├── 03_tables/
│   └── 04_figures/
├── results/
│   ├── figures/
│   ├── tables/
│   ├── estimates/
│   └── presentations/
├── notebooks/
├── docs/
├── references/
├── tests/
├── .gitignore
└── .dvcignore
```

### Key Project Files

| File | Purpose |
|------|---------|
| `config.{py,R,do}` | Centralized paths, random seeds, and lookup functions |
| `run_all.{py,R,do}` | Master script to run full analysis pipeline |
| `citations.md` | Data and software citations per AEA guidelines |
| `_mintd_utils.{py,R,do}` | Path utilities and schema generation helpers |

### Config Lookup Functions

The `config` file includes lookup functions for managing analysis specifications:

```python
# Python example
from config import case2tag, case2vars, pretty_name

tag = case2tag("baseline")        # Returns "base"
spec = case2vars("baseline")      # Returns {"depvar": "...", "controls": [...]}
label = pretty_name("outcome")    # Returns "Outcome Variable"
```

## Standalone Packages (No mintd Scaffolding)

For reusable code packages (Python, R, or Stata), use standard language tooling instead of mintd. Packages have their own conventions that don't need DVC pipelines or data governance metadata.

### When to Create a Package

Code should live inside a `data` repo until there's a reason to extract it. The trigger for extraction is **a second consumer**:

1. You build a data product with specialized code (e.g., HHI calculation)
2. Another project needs the *code*, not just the output
3. Extract the code into a standalone package

### Extraction Checklist

- [ ] Second consumer exists (not hypothetical)
- [ ] Code has clear API boundary (inputs/outputs well-defined)
- [ ] Can be versioned independently of the data pipeline
- [ ] Has tests that run without the full data pipeline

### Package Setup by Language

**Python:**
```bash
uv init my-package  # or: poetry init
```

**R:**
```r
usethis::create_package("mypackage")
```

**Stata:**
Create `.ado` and `.sthlp` files manually.

## Secure Enclave Projects (`enclave_*`)

For air-gapped environments requiring secure data transfer:

```
enclave_secure_workspace/
├── README.md                 # Enclave documentation
├── metadata.json             # Project metadata
├── enclave_manifest.yaml     # Data transfer tracking
├── requirements.txt          # Dependencies
├── data/
│   └── .gitkeep
├── code/
│   ├── __init__.py
│   ├── registry.py           # Registry integration
│   ├── download.py           # Data pulling logic
│   ├── package.py            # Transfer packaging
│   └── verify.py             # Integrity verification
├── scripts/
│   ├── pull_data.sh          # Pull latest data
│   ├── package_transfer.sh   # Create transfer archive
│   ├── unpack_transfer.sh    # Unpack in enclave
│   └── verify_transfer.sh    # Verify checksums
├── transfers/                # Transfer archives
├── .gitignore
└── .dvcignore
```
