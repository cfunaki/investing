"""
SEC EDGAR 13F fetcher and parser.

Fetches and parses 13F-HR filings from SEC EDGAR for institutional
investment managers like Berkshire Hathaway.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# SEC EDGAR API requires a User-Agent with contact info
SEC_USER_AGENT = "InvestingApp/1.0 (contact@example.com)"

# Well-known CIKs
CIK_BERKSHIRE = "0001067983"
CIK_GREENLIGHT = "0001079114"

# CUSIP to ticker mapping for common Berkshire holdings
# This covers the major positions; can be extended as needed
CUSIP_TO_TICKER: dict[str, str] = {
    # Apple
    "037833100": "AAPL",
    # Amazon
    "023135106": "AMZN",
    # Bank of America
    "060505104": "BAC",
    # American Express
    "025816109": "AXP",
    # Coca-Cola
    "191216100": "KO",
    # Citigroup
    "172967424": "C",
    # Chubb (Swiss domicile)
    "H1467J104": "CB",
    # DaVita
    "235811102": "DVA",
    "23918K108": "DVA",
    # Meta (Facebook)
    "30303M102": "META",
    # Goldman Sachs
    "38141G104": "GS",
    # IBM
    "459200101": "IBM",
    # Intel
    "458140100": "INTC",
    # JPMorgan Chase
    "48203R104": "JPM",
    # Kraft Heinz
    "49271V100": "KHC",
    "500754106": "KHC",
    # Louisiana-Pacific
    "531229102": "LPX",
    "531229870": "LPX",
    "546347105": "LPX",
    # Moody's
    "55354G100": "MCO",
    "615369105": "MCO",
    # Microsoft
    "594918104": "MSFT",
    # Newmont
    "637071101": "NEM",
    # Nike
    "654106103": "NKE",
    # NOV Inc
    "655044105": "NOV",
    # Nu Holdings
    "670346105": "NU",
    # Occidental Petroleum
    "68389X105": "OXY",
    "68389X205": "OXY",
    "674599105": "OXY",
    # PepsiCo
    "713448108": "PEP",
    "713448207": "PEP",
    # Procter & Gamble
    "742718109": "PG",
    # SPDR S&P 500 (ETF)
    "78409V104": "SPY",
    # Stanley Black & Decker
    "844741108": "SWK",
    # StoneCo
    "855244109": "STNE",
    # Taiwan Semiconductor
    "872540109": "TSM",
    # TJX Companies
    "882508104": "TJX",
    # U.S. Bancorp
    "904764200": "USB",
    # UnitedHealth
    "91324P102": "UNH",
    # Walmart
    "931142103": "WMT",
    # Visa
    "92826C839": "V",
    "92826C847": "V",
    # Verizon
    "92343V104": "VZ",
    # Wells Fargo
    "929160109": "WFC",
    # VeriSign
    "92553P201": "VRSN",
    "92343E102": "VRSN",
    # Charter Communications
    "127387108": "CHTR",
    "16119P108": "CHTR",
    # Jefferies
    "46625H100": "JEF",
    "47233W109": "JEF",
    # Chevron
    "166764100": "CVX",
    # Alphabet (Google)
    "02079K305": "GOOG",
    "02079K107": "GOOGL",
    # Ally Financial
    "02005N100": "ALLY",
    # Capital One
    "14040H105": "COF",
    # Constellation Brands
    "21036P108": "STZ",
    # Diageo
    "25243Q205": "DEO",
    # Domino's Pizza
    "25754A201": "DPZ",
    # HEICO
    "422806208": "HEI",
    # Kroger
    "501044101": "KR",
    # Lamar Advertising
    "512816109": "LAMR",
    # Lennar
    "526057104": "LEN",
    "526057302": "LEN",
    # Mastercard
    "57636Q104": "MA",
    # NVR
    "62944T105": "NVR",
    # New York Times
    "650111107": "NYT",
    # Pool Corp
    "73278L105": "POOL",
    # Sirius XM
    "829933100": "SIRI",
    # Allegion
    "G0176J109": "ALLE",
    # Aon
    "G0403H108": "AON",
    # Atlanta Braves
    "047726302": "BATRA",
    # Liberty Live
    "530909100": "LLYVK",
    "530909308": "LLYVA",
    # Liberty Media (Formula One)
    "531229755": "FWONK",
    # Liberty Latin America
    "G9001E102": "LILAK",
    "G9001E128": "LILA",
}


@dataclass
class Holding13F:
    """A single holding from a 13F filing."""

    issuer_name: str
    cusip: str
    value: int  # Value in dollars (modern 13F reports in dollars, not thousands)
    shares: int
    investment_discretion: str  # SOLE, SHARED, etc.
    ticker: str | None = None  # Resolved from CUSIP

    @property
    def weight_pct(self) -> float | None:
        """Portfolio weight percentage (set after all holdings loaded)."""
        return getattr(self, "_weight_pct", None)

    @weight_pct.setter
    def weight_pct(self, value: float):
        self._weight_pct = value


@dataclass
class Filing13F:
    """A 13F-HR filing with metadata and holdings."""

    cik: str
    filer_name: str
    report_date: date  # Period of report (quarter end)
    filed_date: date   # When actually filed with SEC
    accession_number: str
    holdings: list[Holding13F]

    @property
    def total_value(self) -> int:
        """Total portfolio value in dollars."""
        return sum(h.value for h in self.holdings)

    @property
    def position_count(self) -> int:
        """Number of positions."""
        return len(self.holdings)


class SECEdgar13FFetcher:
    """
    Fetches and parses 13F-HR filings from SEC EDGAR.

    Uses the SEC EDGAR API to fetch filing metadata and documents.
    Parses the infotable XML to extract holdings.
    """

    EDGAR_BASE = "https://data.sec.gov"
    EDGAR_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"

    def __init__(self, cache_dir: Path | None = None):
        """
        Initialize the fetcher.

        Args:
            cache_dir: Directory to cache downloaded filings.
                      Defaults to data/raw/13f/
        """
        self.cache_dir = cache_dir or Path("data/raw/13f")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    async def fetch_latest_13f(self, cik: str) -> Filing13F | None:
        """
        Fetch the most recent 13F-HR filing for a CIK.

        Args:
            cik: SEC Central Index Key (padded to 10 digits)

        Returns:
            Filing13F with holdings, or None if not found
        """
        log = logger.bind(cik=cik)
        log.info("fetching_latest_13f")

        # Ensure CIK is zero-padded to 10 digits
        cik = cik.zfill(10)

        async with httpx.AsyncClient(
            headers={"User-Agent": SEC_USER_AGENT},
            timeout=30.0,
        ) as client:
            # Step 1: Get recent filings list
            filings_url = f"{self.EDGAR_BASE}/submissions/CIK{cik}.json"
            log.debug("fetching_filings_list", url=filings_url)

            response = await client.get(filings_url)
            if response.status_code != 200:
                log.error(
                    "failed_to_fetch_filings",
                    status_code=response.status_code,
                )
                return None

            filings_data = response.json()
            filer_name = filings_data.get("name", "Unknown")

            # Step 2: Find most recent 13F-HR
            recent_filings = filings_data.get("filings", {}).get("recent", {})
            forms = recent_filings.get("form", [])
            accession_numbers = recent_filings.get("accessionNumber", [])
            filing_dates = recent_filings.get("filingDate", [])
            report_dates = recent_filings.get("reportDate", [])

            # Find first 13F-HR
            filing_idx = None
            for i, form in enumerate(forms):
                if form == "13F-HR":
                    filing_idx = i
                    break

            if filing_idx is None:
                log.warning("no_13f_filing_found")
                return None

            accession = accession_numbers[filing_idx]
            filed_date = datetime.strptime(filing_dates[filing_idx], "%Y-%m-%d").date()
            report_date = datetime.strptime(report_dates[filing_idx], "%Y-%m-%d").date()

            log.info(
                "found_13f_filing",
                accession=accession,
                filed_date=str(filed_date),
                report_date=str(report_date),
            )

            # Step 3: Get filing documents to find information table
            accession_clean = accession.replace("-", "")
            cik_numeric = cik.lstrip('0')

            # Fetch the filing index HTML to find the information table file
            index_url = (
                f"{self.EDGAR_ARCHIVES}/{cik_numeric}/{accession_clean}/"
                f"{accession}-index.htm"
            )

            log.debug("fetching_filing_index", url=index_url)
            response = await client.get(index_url)

            infotable_file = None
            if response.status_code == 200:
                # Parse the HTML to find "INFORMATION TABLE" file
                # Look for rows containing "INFORMATION TABLE" and extract the XML filename
                import re
                html = response.text

                # Pattern: find XML files associated with "INFORMATION TABLE"
                # The HTML structure has rows with links followed by description
                # Look for XML files that aren't the primary_doc.xml
                matches = re.findall(
                    r'href="[^"]*?/([^/"]+\.xml)"[^>]*>[^<]*</a>\s*</td>\s*<td[^>]*>INFORMATION TABLE',
                    html,
                    re.IGNORECASE,
                )
                if matches:
                    # Take the first match that isn't primary_doc.xml
                    for match in matches:
                        if "primary_doc" not in match.lower():
                            infotable_file = match
                            break

                # Alternative pattern: sometimes it's just listed as a .xml file
                if not infotable_file:
                    matches = re.findall(
                        r'href="[^"]*?/(\d+\.xml)"',
                        html,
                    )
                    if matches:
                        infotable_file = matches[0]

            if not infotable_file:
                # Fallback: try common names
                for fallback in ["infotable.xml", "form13fInfoTable.xml"]:
                    test_url = f"{self.EDGAR_ARCHIVES}/{cik_numeric}/{accession_clean}/{fallback}"
                    test_response = await client.head(test_url)
                    if test_response.status_code == 200:
                        infotable_file = fallback
                        break

            if not infotable_file:
                log.error("could_not_find_infotable", accession=accession)
                return None

            infotable_url = (
                f"{self.EDGAR_ARCHIVES}/{cik_numeric}/{accession_clean}/"
                f"{infotable_file}"
            )

            # Step 4: Fetch and parse infotable
            log.debug("fetching_infotable", url=infotable_url)
            response = await client.get(infotable_url)

            if response.status_code != 200:
                log.error(
                    "failed_to_fetch_infotable",
                    status_code=response.status_code,
                    url=infotable_url,
                )
                return None

            # Cache the XML
            cache_file = self.cache_dir / f"{cik}_{accession_clean}_infotable.xml"
            cache_file.write_bytes(response.content)
            log.debug("cached_infotable", path=str(cache_file))

            # Parse holdings
            raw_holdings = self._parse_infotable_xml(response.content)

            # Aggregate holdings by CUSIP (combine different discretion entries)
            holdings = self._aggregate_holdings(raw_holdings)
            log.info(
                "aggregated_holdings",
                raw_count=len(raw_holdings),
                aggregated_count=len(holdings),
            )

            # Calculate weights
            total_value = sum(h.value for h in holdings)
            for h in holdings:
                h.weight_pct = (h.value / total_value * 100) if total_value > 0 else 0

            # Resolve tickers
            for h in holdings:
                h.ticker = CUSIP_TO_TICKER.get(h.cusip)
                if not h.ticker:
                    log.debug(
                        "unknown_cusip",
                        cusip=h.cusip,
                        issuer=h.issuer_name,
                    )

            return Filing13F(
                cik=cik,
                filer_name=filer_name,
                report_date=report_date,
                filed_date=filed_date,
                accession_number=accession,
                holdings=holdings,
            )

    def _aggregate_holdings(self, holdings: list[Holding13F]) -> list[Holding13F]:
        """
        Aggregate holdings by CUSIP.

        13F filings can have multiple entries for the same security
        with different investment discretion types (SOLE, SHARED, etc.)
        or different sub-managers. This combines them into single entries.
        """
        by_cusip: dict[str, Holding13F] = {}

        for h in holdings:
            if h.cusip in by_cusip:
                # Aggregate: sum values and shares
                existing = by_cusip[h.cusip]
                by_cusip[h.cusip] = Holding13F(
                    issuer_name=existing.issuer_name,
                    cusip=existing.cusip,
                    value=existing.value + h.value,
                    shares=existing.shares + h.shares,
                    investment_discretion="AGGREGATED",
                    ticker=existing.ticker,
                )
            else:
                by_cusip[h.cusip] = h

        return list(by_cusip.values())

    def _parse_infotable_xml(self, xml_content: bytes) -> list[Holding13F]:
        """
        Parse the 13F infotable XML to extract holdings.

        The XML structure is:
        <informationTable>
          <infoTable>
            <nameOfIssuer>APPLE INC</nameOfIssuer>
            <titleOfClass>COM</titleOfClass>
            <cusip>037833100</cusip>
            <value>157823000</value>  <!-- in dollars -->
            <shrsOrPrnAmt>
              <sshPrnamt>400000000</sshPrnamt>
              <sshPrnamtType>SH</sshPrnamtType>
            </shrsOrPrnAmt>
            <investmentDiscretion>SOLE</investmentDiscretion>
            <votingAuthority>
              <Sole>400000000</Sole>
              <Shared>0</Shared>
              <None>0</None>
            </votingAuthority>
          </infoTable>
          ...
        </informationTable>
        """
        holdings = []

        # Parse XML
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as e:
            logger.error("xml_parse_error", error=str(e))
            return holdings

        # Handle namespace
        # The XML typically has namespace like:
        # xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable"
        ns = {"ns": "http://www.sec.gov/edgar/document/thirteenf/informationtable"}

        # Try with namespace first
        info_tables = root.findall(".//ns:infoTable", ns)
        if not info_tables:
            # Try without namespace
            info_tables = root.findall(".//infoTable")

        for info in info_tables:
            try:
                # Extract with namespace
                def get_text(elem: ET.Element, tag: str) -> str:
                    child = elem.find(f"ns:{tag}", ns)
                    if child is None:
                        child = elem.find(tag)
                    return child.text if child is not None and child.text else ""

                issuer_name = get_text(info, "nameOfIssuer")
                cusip = get_text(info, "cusip")
                value_str = get_text(info, "value")
                discretion = get_text(info, "investmentDiscretion")

                # Get shares from nested element
                shrs_elem = info.find("ns:shrsOrPrnAmt", ns)
                if shrs_elem is None:
                    shrs_elem = info.find("shrsOrPrnAmt")

                shares = 0
                if shrs_elem is not None:
                    shrs_amt = shrs_elem.find("ns:sshPrnamt", ns)
                    if shrs_amt is None:
                        shrs_amt = shrs_elem.find("sshPrnamt")
                    if shrs_amt is not None and shrs_amt.text:
                        shares = int(shrs_amt.text)

                holdings.append(
                    Holding13F(
                        issuer_name=issuer_name,
                        cusip=cusip,
                        value=int(value_str) if value_str else 0,
                        shares=shares,
                        investment_discretion=discretion,
                    )
                )
            except (ValueError, AttributeError) as e:
                logger.warning("failed_to_parse_holding", error=str(e))
                continue

        logger.info("parsed_holdings", count=len(holdings))
        return holdings

    def resolve_cusip_to_ticker(
        self, cusip: str, issuer_name: str | None = None
    ) -> str | None:
        """
        Resolve a CUSIP to ticker symbol.

        Uses the built-in mapping first, could be extended to use
        OpenFIGI or other services.

        Args:
            cusip: 9-digit CUSIP
            issuer_name: Issuer name for fallback matching

        Returns:
            Ticker symbol or None if not found
        """
        return CUSIP_TO_TICKER.get(cusip)


def weight_bucket_to_integer(weight_pct: float) -> int:
    """
    Convert Berkshire portfolio weight percentage to integer weight
    for fixed-dollar allocation.

    | Berkshire Weight | Assigned Weight | Target $ at $500/weight |
    |------------------|-----------------|-------------------------|
    | >= 20%           | 8               | $4,000                  |
    | 15-20%           | 7               | $3,500                  |
    | 10-15%           | 6               | $3,000                  |
    | 7-10%            | 5               | $2,500                  |
    | 5-7%             | 4               | $2,000                  |
    | 3-5%             | 3               | $1,500                  |
    | < 3%             | excluded (0)    | -                       |

    Args:
        weight_pct: Portfolio weight as percentage (e.g., 25.5 for 25.5%)

    Returns:
        Integer weight for fixed-dollar allocation (0 if excluded)
    """
    if weight_pct >= 20:
        return 8
    elif weight_pct >= 15:
        return 7
    elif weight_pct >= 10:
        return 6
    elif weight_pct >= 7:
        return 5
    elif weight_pct >= 5:
        return 4
    elif weight_pct >= 3:
        return 3
    else:
        return 0  # Excluded
