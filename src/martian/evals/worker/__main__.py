"""``python -m martian.evals.worker``: one run's process, started by ``ProcessWorker``.

See :mod:`martian.evals.worker.process`.
"""

import sys

from martian.evals.worker.process import main

if __name__ == "__main__":
    sys.exit(main())
