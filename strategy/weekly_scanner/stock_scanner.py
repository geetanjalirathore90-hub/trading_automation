from pathlib import Path

import pandas as pd


DEFAULT_OUTPUT_FILE = "weekly_analysis.xlsx"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "output_files"


def save_placeholder(output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    placeholder = pd.DataFrame(
        [
            {
                "status": "PENDING",
                "message": "Stock scanner rules not implemented yet.",
            }
        ]
    )

    if output_path.exists():
        with pd.ExcelWriter(output_path, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
            placeholder.to_excel(writer, sheet_name="stock_scan", index=False)
    else:
        with pd.ExcelWriter(output_path, engine="openpyxl", mode="w") as writer:
            placeholder.to_excel(writer, sheet_name="stock_scan", index=False)


def main() -> None:
    output_file = OUTPUT_DIR / DEFAULT_OUTPUT_FILE
    save_placeholder(output_file)
    print(f"Placeholder stock scan saved to: {output_file}")


if __name__ == "__main__":
    main()
