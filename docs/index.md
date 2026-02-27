# Mintd: Effortless Research Project Management

`mintd` is a command-line tool designed for social science researchers who need to organize their work, ensure reproducibility, and collaborate seamlessly. It automates the tedious parts of project setup so you can focus on your analysis.

## Why use `mintd`?

### 📁 Standardized Research Workflows
Stop worrying about how to organize your folders. `mintd` creates a consistent, lab-standard structure for every project, making it easy for you and your collaborators to find data, scripts, and results.

### 🛡️ Built-in Reproducibility
`mintd` automatically initializes version control for both your code (Git) and your data (DVC). Every time you run an analysis, `mintd` helps you track exactly which version of the data was used, ensuring your results can be audited and replicated.

### 🛠️ Multi-Tool Compatibility
Whether you prefer **Stata**, **R**, or **Python**, `mintd` has you covered. It generates language-specific templates and utilities, including native Stata commands and automated logging, so your workflow stays consistent across tools.

### 🌐 Seamless Data Commons Integration
Automatically register your projects with the **Data Commons Registry**. `mintd` handles the technical details of cataloging and permissions in the background, making it easier to share your work with the lab without managing complex security tokens.

---

## Get Started in Seconds

```bash
# Install mintd
pip install mintd

# Create a new project
mintd create data --name my-research-project --lang python
```

Next: [Installation Guide](installation.md) | [Quick Start](quick-start.md)
