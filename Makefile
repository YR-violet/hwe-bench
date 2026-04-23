.PHONY: help format check lint fix clean install-dev

# Default target
help:
	@echo "Available commands:"
	@echo "  make install-dev - Install development dependencies"
	@echo "  make install   - Install hwe-bench package"
	@echo "  make format    - Format all Python files using ruff"
	@echo "  make check     - Check code formatting without making changes"
	@echo "  make lint      - Run ruff linter to check for code issues"
	@echo "  make fix       - Auto-fix linting issues using ruff"
	@echo "  make clean     - Clean up Python cache files"

# Install development dependencies
install-dev:
	@echo "Installing development dependencies..."
	pip install -e ".[dev]"

# Install hwe-bench package
install:
	@echo "Installing hwe-bench..."
	pip install -e .

# Format all Python files
format:
	@echo "Formatting Python files with ruff..."
	ruff format .

# Check formatting without making changes
check:
	@echo "Checking code formatting..."
	ruff format --check .

# Run linter to check for code issues
lint:
	@echo "Running ruff linter..."
	ruff check .

# Auto-fix linting issues
fix:
	@echo "Auto-fixing linting issues with ruff..."
	ruff check --fix .

# Clean up Python cache files
clean:
	@echo "Cleaning up Python cache files..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type f -name "*.pyo" -delete 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
