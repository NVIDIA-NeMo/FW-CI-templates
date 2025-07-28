# NeMo Github Repo Template

 Install the [cookiecutter](https://github.com/cookiecutter/cookiecutter) package and run `cookiecutter path/to/github_repo_template` to create an initial NeMo Github repo.

# Secrets Auto-Detector Instructions : How To Generate the Secrets Baseline for a Repository

Run:
`detect-secrets scan --exclude-files 'pyproject\.toml|\.github/workflows/config/\.secrets\.baseline' > .github/workflows/config/.secrets.baseline`

Note in the above `pyproject.toml` and `.github/workflows/config/.secrets.baseline` are examples of files wanted to be ignored.
