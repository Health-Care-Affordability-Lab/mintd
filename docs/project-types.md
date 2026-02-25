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

## Infrastructure Projects (`infra_*`)

For reusable packages and tools:

**Python:**
```
infra_stat_tools/
├── README.md
├── metadata.json
├── pyproject.toml            # Package configuration
├── data/
│   ├── raw/
│   └── analysis/
├── code/
│   └── stat_tools/           # Main package
│       └── __init__.py
├── tests/
│   └── __init__.py
├── docs/
└── .gitignore
```

**R:**
```
infra_stat_tools/
├── README.md
├── metadata.json
├── DESCRIPTION               # R package description
├── NAMESPACE                 # R namespace exports
├── data/
│   ├── raw/
│   └── analysis/
├── code/
│   └── stat_tools.R          # Package functions
├── tests/
├── docs/
└── .gitignore
```

**Stata:**
```
infra_stat_tools/
├── README.md
├── metadata.json
├── data/
│   ├── raw/
│   └── analysis/
├── code/
│   ├── stat_tools.ado        # Stata command
│   └── stat_tools.sthlp      # Help file
├── tests/
├── docs/
└── .gitignore
```

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
