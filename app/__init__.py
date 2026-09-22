import os
from importlib.metadata import PackageNotFoundError, version

# Derived from the nearest git tag by hatch-vcs (see pyproject.toml) at
# install/build time - no manual version bumps. Package metadata is absent
# when running straight from a Docker image (which ships raw source rather
# than an installed package, see Dockerfile), so LNURLCASH_ORACLE_VERSION
# is baked in there instead, from the same git tag, at image build time.
try:
    __version__ = version("lnurlcash-oracle")
except PackageNotFoundError:
    __version__ = os.environ.get("LNURLCASH_ORACLE_VERSION", "0.0.0+unknown")
