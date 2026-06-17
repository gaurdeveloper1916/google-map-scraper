import argparse
import logging
from dataclasses import asdict

import pandas as pd

from scraper import scrape_places




def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )




def save_places_to_csv(places, output):
    df = pd.DataFrame([asdict(p) for p in places])

    if df.empty:
        print("No data found")
        return

    df.drop_duplicates(subset=["name", "phone_number"], inplace=True)
    df.to_csv(output, index=False)

    print(df)
    print(f"\nSaved → {output}")




def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--search", required=True)
    parser.add_argument("-t", "--total", type=int, default=100)
    parser.add_argument("-o", "--output", default="result.csv")
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger(__name__)

    data = scrape_places(args.search, args.total, logger=logger)
    save_places_to_csv(data, args.output)


if __name__ == "__main__":
    main()
