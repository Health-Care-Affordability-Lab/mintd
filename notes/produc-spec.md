# PRD: mint (Lab Project Scaffolding Tool)

## 1. Product overview

### 1.1 Document title and version
   - PRD: mint (Lab Project Scaffolding Tool)
   - Version: 1.0.0 (Registry Integration Complete)

### 1.2 Product summary
`mint` is a Python-based scaffolding package designed to standardize data science and economic research projects within the lab. It automates the creation of standardized GitHub repositories (`data_`, `prj__`, `infra_`, `enclave_`) with pre-configured Git and DVC initialization.

Crucially, `mint` acts as the client-side enforcer for the lab's "Data Commons." It utilizes a GitOps workflow to automatically register new projects into a central Registry Repository and enforces access control policies via code. This ensures that while researchers focus on "getting stuff done," compliance and cataloging happen transparently in the background. The tool is accessible via both a Python CLI and a native Stata command (`prjsetup`) for the primary user base of non-technical researchers.

**✅ REGISTRY INTEGRATION COMPLETE**: mint now includes full tokenless GitOps-based project registration using SSH keys and GitHub CLI, enabling seamless project cataloging without requiring personal access tokens.

## 2. Goals

### 2.1 Business goals
   - **100% Catalog Coverage**: Ensure every new data product or analysis project is automatically logged in the central Data Commons Registry.
   - **Policy-as-Code Enforcement**: Replace manual repository configuration with automated, code-defined access policies managed via GitHub Actions.
   - **Standardization**: Enforce uniform directory structures across the lab to facilitate code review and reproducibility for senior staff.

### 2.2 User goals
   - **Frictionless Setup**: Allow junior researchers to spin up a fully compliant environment (Repo + S3 + Permissions) in under 2 minutes.
   - **Tooling Agnostic Entry**: Enable users to create projects from their preferred environment (Stata or Terminal) without needing DevOps knowledge.
   - **Automated "Plumbing"**: Remove the need for users to manually configure S3 buckets, DVC remotes, or API tokens.

### 2.3 Non-goals
   - Building a custom web UI for the Data Registry (MVP will rely on GitHub's native file viewing and PR interface).
   - Handling data ingestion logic or ETL pipelines (Scope is limited to scaffolding and registration).
   - Supporting cloud providers other than Wasabi S3 and GitHub for the MVP.

## 3. User personas

### 3.1 Key user types
   - Junior Researcher (Primary)
   - Senior Research Reviewer
   - Data Infrastructure Admin

### 3.2 Basic persona details
   - **Junior Researcher**: Non-technical domain experts (Economists/Analysts). They value speed and "getting stuff done" over adhering to complex guidelines. They work primarily in Stata and view technical configuration as a blocker.
   - **Senior Research Reviewer**: Does not code but reviews outputs (tables, figures). Needs consistent folder structures to locate results quickly without asking "where is the file?".
   - **Data Infrastructure Admin**: Manages security and compliance. Needs a "God-view" of all data assets and guaranteed enforcement of access policies without manual auditing.

### 3.3 Role-based access
   - **Junior Researcher**: Can run `mint create` and `prjsetup`. Has Read/Write access to their own projects. Can view the Registry but cannot approve global policy changes.
   - **Senior Research Reviewer**: Has Read-only access to `prj__` repositories to review outputs.
   - **Data Infrastructure Admin**: Has Admin access to the Registry Repository. Can merge Pull Requests that alter access policies and update infrastructure templates.

## 4. Functional requirements
   - **CLI Scaffolding** (Priority: High)
     - Generate directory trees for `data_` (products), `prj__` (analysis), `infra_` (tooling), and `enclave_` (secure data consumption).
     - Render language-specific templates for `README.md`, `metadata.json`, `.gitignore`, and utility scripts.
     - **Language Selection**: Require explicit programming language choice (Python/R/Stata) with no defaults.
   - **Mint Utilities** (Priority: High)
     - Auto-generate `_mint_utils.{lang}` files with project validation, logging, and schema utilities.
     - **Project Directory Validation**: Ensure scripts run from correct project root with clear error messages.
     - **Parameter-Aware Logging**: Create timestamped log files with script parameters (e.g., `ingest_2023.log`).
     - **Schema Generation**: Extract variable metadata, types, and observation counts for data registry.
     - **Version Tracking**: Include mint version and commit hash in project metadata.
   - **Enclave Data Access** (Priority: High)
     - Registry integration to discover approved data products for secure enclaves.
     - Automated download and organization of versioned data products in `data/repo/hash-date` structure.
     - Data integrity validation and access logging for compliance.
   - **Git & DVC Initialization** (Priority: High)
     - Initialize local Git repositories.
     - Initialize DVC and configure the remote to specific Wasabi S3 buckets (`s3://bucket/{project_name}`).
   - **Registry Integration (GitOps)** (Priority: High)
     - Automatically generate a metadata YAML file upon project creation.
     - Create a Pull Request to the central `data-commons-registry` repo to register the new asset.
   - **Stata Wrapper (`prjsetup`)** (Priority: High)
     - A native Stata command that wraps the Python CLI, allowing creation of projects directly from the Stata console.
   - **Registry Integration (GitOps)** (Priority: High)
     - Automatically generate a metadata YAML file upon project creation.
     - Create a Pull Request to the central `data-commons-registry` repo to register the new asset.
     - Provide CLI commands for manual registration, status checking, and metadata updates.
   - **Access Policy Enforcement** (Priority: High)
     - Backend logic (GitHub Action) to sync the permissions defined in the Registry YAML with actual GitHub Repository Team permissions.

## 5. User experience

### 5.1. Entry points & first-time user flow
   - **Python CLI**: Users install via `pip install mint`. First run triggers `mint config` to set up S3 storage credentials and registry URL.
   - **Stata**: Users install via `ssc install mint` (or local net install). The command `prjsetup` detects if the Python package is missing and prompts to install it.

   **Prerequisites for Registry Integration:**
   - SSH key configured for GitHub
   - GitHub CLI (`gh`) installed and authenticated
   - Push access to the registry repository

### 5.2. Core experience
   - **Create Project**:
     - User types `prjsetup, type(data) name(medicare_2024) lang(stata)` in Stata.
     - System displays a spinner: "Scaffolding folders... Initializing DVC... Registering with Data Commons..."
     - System creates project structure with Git/DVC setup and language-specific utilities.
     - System clones registry, creates catalog entry, and opens PR via git/SSH.
     - System returns: "Success! Project created at ./data_medicare_2024. Registration PR: https://github.com/org/registry/pull/123".
     - User immediately begins work in the created folder with pre-configured logging and utilities.

### 5.3. Advanced features & edge cases
   - **Offline Mode**: If the Registry is unreachable, the tool scaffolds locally and warns the user to run `mint register` later.
   - **Naming Conflicts**: If a project name already exists in the Registry, the CLI prompts the user to choose a different name or import the existing project.
   - **Registry Commands**: Dedicated commands for managing project registrations:
     - `mint register` - Register an existing project with the registry
     - `mint status` - Check registration status and view registry entry
     - `mint update-registry` - Update project metadata in the registry

### 5.4. UI/UX highlights
   - **Minimalist Feedback**: Success messages provide direct clickable links to the Repo and the Registry entry.
   - **Stata-Native Feel**: The Stata wrapper uses standard Stata syntax and error reporting, hiding the Python complexity completely.

## 6. Narrative
Sarah is a Junior Researcher who needs to start analyzing 2024 Medicare claims. In the past, she would copy-paste an old folder, struggle with S3 credentials, and eventually lose track of which data version she used. With `mint`, she simply opens Stata and types `prjsetup, type(data) name(medicare_24)`. Within 30 seconds, her folder is created with a standard structure, DVC is ready to track her large files, and the project is automatically registered in the lab's central catalog. She doesn't need to read a wiki or ask an admin for permissions—she just gets to work. Later, her Senior Reviewer can easily find her output tables because the folder structure is exactly the same as every other project in the lab.

For sensitive analyses requiring secure enclave access, Sarah can create an enclave workspace by typing `prjsetup, type(enclave) name(confidential_study)`. This creates a controlled environment where approved data products are automatically downloaded and organized in versioned directories, ensuring compliance with data governance policies while maintaining analytical workflow continuity.

## 7. Success metrics

### 7.1. User-centric metrics
   - **Adoption Rate**: % of new projects created using `mint` vs. manual creation (Target: >90% within 3 months).
   - **Time-to-Start**: Average time from intent to "ready to code" (Target: < 2 minutes).

### 7.2. Business metrics
   - **Registry Completeness**: % of active repositories that have a corresponding entry in the Data Commons Registry.
   - **Support Ticket Reduction**: Decrease in "I can't access this data" or "Where is the repo?" inquiries to the Admin.

### 7.3. Technical metrics
   - **Registration Success Rate**: % of `mint create` executions that successfully merge a PR to the Registry.
   - **Policy Sync Latency**: Time between Registry update and GitHub permission application (Target: < 5 minutes).

## 8. Technical considerations

### 8.1. Integration points
   - **GitOps Registry**: Tokenless registration using SSH + GitHub CLI for creating PRs in registry repository.
   - **Git**: Direct git commands for cloning registry and creating branches/commits.
   - **GitHub CLI**: `gh pr create` command for opening pull requests.
   - **Data Commons Registry**: Git-based catalog system for tracking all lab projects and their metadata.
   - **Wasabi S3**: Used for DVC remote storage with per-project bucket creation.
   - **Local Stata Integration**: Requires Python integration (`pystata` or shell calls) within Stata.

### 8.2. Data storage & privacy
   - **Metadata**: Stored in the private `data-commons-registry` GitHub repo (YAML format).
   - **Data Files**: Stored in private Wasabi S3 buckets; never committed to Git.
   - **Credentials**: S3 credentials stored securely in local OS keychain or environment variables. Registry access uses SSH keys (no token storage required).

### 8.3. Scalability & performance
   - **Async Registration**: The "Registration" step opens a PR but does not block the user from working locally if the network is slow.
   - **DVC**: Handles TB-scale datasets efficiently, decoupling data size from Git repo size.

### 8.4. Potential challenges
   - **Stata Python Path**: Ensuring Stata can find the correct Python environment where `mint` is installed.
   - **SSH Key Setup**: Users need SSH keys configured for GitHub to enable tokenless registration.
   - **GitHub CLI Installation**: Users need `gh` CLI installed and authenticated for PR creation.
   - **Registry Access**: Users need push access to the registry repository.

## 9. Milestones & sequencing

### 9.1. Project estimate
   - **Medium**: 4-6 weeks for MVP release.

### 9.2. Team size & composition
   - **Small Team**: 1-3 people
     - 1 Lead Engineer (Python/DevOps/GitOps)
     - 1 Stata Specialist (Consultant/Internal User) to refine the `.ado` wrapper.
     - 1 Product Owner (Data Admin) for requirements and testing.

### 9.3. Suggested phases
   - **Phase 1**: Core Python CLI & Project Scaffolding (2 weeks)
     - Key deliverables: `mint` Python package, project templates, Git/DVC initialization.
   - **Phase 2**: Registry Integration & GitOps (2 weeks)
     - Key deliverables: RegistryClient class, GitHub API integration, catalog YAML generation, registry CLI commands.
   - **Phase 3**: Stata Integration & Docs (2 weeks)
     - Key deliverables: `prjsetup.ado` command, Stata help files, installation guide.
   - **Phase 3**: Beta Rollout & Training (1-2 weeks)
     - Key deliverables: Workshop for Junior Researchers, internal "Gold Standard" repo examples.

## 10. User stories

### 10.1. Initialize Data Project (Python)
   - **ID**: US-001
   - **Description**: As a Data Engineer, I want to create a new data project via the CLI so that the standard directory structure and DVC remotes are configured automatically.
   - **Acceptance criteria**:
     - Command `mint create data --name {name} --lang python` creates the folder structure.
     - `dvc status` shows the remote is configured to `s3://bucket/{name}` with language-specific commands.
     - A `metadata.json` file is populated with the project name, creator, and mint version info.
     - `_mint_utils.py` is auto-generated with logging and schema utilities.

### 10.2. Initialize Data Project (Stata)
   - **ID**: US-002
   - **Description**: As a Junior Researcher, I want to set up a new project directly from Stata so that I don't have to use the terminal.
   - **Acceptance criteria**:
     - Running `prjsetup, type(data) name(analysis_1) lang(stata)` in Stata creates the project.
     - Stata Output window confirms success and prints the path.
     - `_mint_utils.do` is auto-generated with parameter-aware logging utilities.
     - The underlying Python command handles all errors gracefully and reports them back to Stata.

### 10.3. Automated Registry Registration
   - **ID**: US-003
   - **Description**: As a Data Admin, I want every new project to automatically open a PR to the Registry so that I can maintain a central catalog without manual data entry.
   - **Acceptance criteria**:
     - Upon `mint create`, a new branch is created in `data-commons-registry`.
     - A YAML file `{project_name}.yaml` is added to the `/catalog` folder.
     - A Pull Request is opened with the title "Register: {project_name}".

### 10.4. Registry Management
   - **ID**: US-004
   - **Description**: As a Data Admin, I want to manage project registrations through dedicated CLI commands so that I can register projects, check status, and update metadata.
   - **Acceptance criteria**:
     - `mint register --path /project/dir` creates a PR in the registry repo.
     - `mint status --name project_name` shows registration status and PR URL if pending.
     - `mint update-registry --name project_name --description "new desc"` creates an update PR.

### 10.5. Secure Access Enforcement
   - **ID**: US-005
   - **Description**: As a Security Officer, I want project access to be managed by the Registry's YAML files so that permissions are auditable and strictly enforced.
   - **Acceptance criteria**:
     - When the Registry PR is merged, a GitHub Action triggers.
     - The Action reads the `access_control` section of the YAML.
     - The Action updates the actual GitHub Repository settings to match the teams defined (e.g., adding `health-econ-team` as Readers).

### 10.6. Initialize Enclave Workspace (Python)
   - **ID**: US-006
   - **Description**: As a Researcher working in a secure enclave, I want to create an enclave workspace via the CLI so that I can access approved data products in a controlled environment.
   - **Acceptance criteria**:
     - Command `mint create enclave --name {name}` creates the enclave workspace structure.
     - Registry integration allows discovery of approved data products.
     - Data organization follows `data/repo/hash-date` structure for versioned access.

### 10.7. Initialize Enclave Workspace (Stata)
   - **ID**: US-007
   - **Description**: As a Researcher working in a secure enclave, I want to set up an enclave workspace directly from Stata so that I can access sensitive data without leaving my analysis environment.
   - **Acceptance criteria**:
     - Running `prjsetup, type(enclave) name(secure_analysis)` in Stata creates the enclave workspace.
     - Stata Output window confirms success and shows available data products.
     - Registry queries work within the secure enclave environment.

### 10.8. Automated Data Product Download
   - **ID**: US-008
   - **Description**: As a Data Steward in a secure enclave, I want automated scripts to download approved data products so that data access is controlled and auditable.
   - **Acceptance criteria**:
     - Enclave workspace includes scripts to query registry for approved data products.
     - Data downloads are organized in `data/{repo_name}/{hash}-{date}/` directories.
     - Download logs and integrity checks are maintained for compliance.
