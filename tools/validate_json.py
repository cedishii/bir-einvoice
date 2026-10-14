r"""Check any e-invoice JSON file against the validator.

Usage:
    .venv\Scripts\python tools\validate_json.py FILE.json [--cert 20ABC123] [--test]

--cert  the EIS Cert Number the file should carry (default: EIS_CERT_NO from .env)
--test  allow the dummy TEST values (for TEST-*.json files)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from einvoice.config import TEST_EIS_CERT_NO, load_config  # noqa: E402
from einvoice.eis.validator import Context, validate_text  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path)
    parser.add_argument("--cert")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    cert = args.cert or (TEST_EIS_CERT_NO if args.test else load_config().eis_cert_no)
    issues = validate_text(args.file.read_text(encoding="utf-8"),
                           Context(eis_cert_no=cert, test_mode=args.test))
    for issue in issues:
        print(issue)
    print(f"{args.file.name}: {'OK' if not issues else f'{len(issues)} problem(s)'}")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
