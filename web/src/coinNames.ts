// Display names for instrument tickers, used where a bare ticker is unclear (e.g. FET is
// Fetch.AI). Display only: nothing here is an input to any gate, check or verdict.
// Add a line when a new instrument is watched; unknown tickers show as the ticker itself.
const COIN_NAMES: Record<string, string> = {
  BTC: "Bitcoin",
  ETH: "Ethereum",
  SOL: "Solana",
  ZEC: "Zcash",
  XRP: "XRP",
  TAO: "Bittensor",
  HEMI: "Hemi",
  FET: "Fetch.AI",
};

export function coinName(ticker: string): string {
  return COIN_NAMES[ticker.toUpperCase()] ?? ticker;
}
