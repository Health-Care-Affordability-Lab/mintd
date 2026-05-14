import argparse

def main():
    parser = argparse.ArgumentParser(
        prog="mintd",
        description="Mintd: Lightweight data product framework for research labs",
    )

    # Placeholder for future subcommands or arguments
    parser.add_argument(
        "--version", action="version", version="%(prog)s 0.0.1"
    )

    args = parser.parse_args()

if __name__ == "__main__":
    main()
