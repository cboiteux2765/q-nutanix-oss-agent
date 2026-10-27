import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import fixtures as fx  # noqa: E402
from app import gitutil  # noqa: E402
from app.checks import CheckContext  # noqa: E402
from app.models import Policy  # noqa: E402
from app.risk import classify  # noqa: E402


@pytest.fixture
def make_ctx(tmp_path):
    """Build a fixture repo + PR and return a CheckContext for it."""

    def _make(changes, extra_base=None, policy=None, advisories_path=None):
        repo, base = fx.make_repo(tmp_path, extra_base)
        head = fx.open_pr(repo, changes)
        files = gitutil.changed_files(repo, base, head)
        pol = policy or Policy()
        tier, _ = classify(files, pol)
        return CheckContext(
            repo=repo,
            base_sha=base,
            head_sha=head,
            files=files,
            added=gitutil.added_lines(repo, base, head),
            policy=pol,
            tier=tier,
            advisories_path=advisories_path,
        )

    return _make
