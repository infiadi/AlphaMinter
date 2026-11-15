import logging
import requests
from typing import Dict, Any, Optional, List

try:
    import yfinance as yf
except ImportError:
    yf = None

logger = logging.getLogger("brokerage_targets")

_http_session = None


def get_yfinance_session() -> requests.Session:
    """Returns a pooled requests.Session to force HTTP/1.1 and avoid curl_cffi HTTP2 framing errors."""
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
        _http_session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
    return _http_session


def fetch_brokerage_consensus(symbol: str) -> Dict[str, Any]:
    """
    Fetches consensus analyst price targets, forward multiples, EPS revisions,
    and recent news from Yahoo Finance for Indian equities.
    Tries NSE suffix (.NS) first, then BSE (.BO).
    Returns a standardized dictionary.
    """
    clean_sym = symbol.strip().upper()
    data: Dict[str, Any] = {
        "symbol": clean_sym,
        "has_coverage": False,
        "target_median": None,
        "target_mean": None,
        "target_high": None,
        "target_low": None,
        "upside_pct": None,
        "analyst_count": 0,
        "recommendation": None,
        "forward_pe": None,
        "forward_eps": None,
        "beta": None,
        "ev_to_ebitda": None,
        "institutional_holding_pct": None,
        "eps_revisions_30d": None,
        "eps_trend_30d": None,
        "recent_news": [],
        "yahoo_url": f"https://finance.yahoo.com/quote/{clean_sym}.NS"
    }

    if yf is None:
        return data

    ticker_keys = [f"{clean_sym}.NS", f"{clean_sym}.BO"]
    t_obj = None
    info = None

    session = get_yfinance_session()
    for tk in ticker_keys:
        try:
            candidate_t = yf.Ticker(tk, session=session)
            fetched_info = candidate_t.info
            # Verify valid data returned
            if fetched_info and (fetched_info.get("targetMeanPrice") or fetched_info.get("currentPrice") or fetched_info.get("regularMarketPrice")):
                info = fetched_info
                t_obj = candidate_t
                break
        except Exception as e:
            logger.debug(f"Failed fetching {tk} from yfinance: {e}")

    if not info or not t_obj:
        return data

    cmp = info.get("currentPrice") or info.get("regularMarketPrice")
    median_target = info.get("targetMedianPrice") or info.get("targetMeanPrice")
    analyst_count = info.get("numberOfAnalystOpinions") or 0

    if median_target and median_target > 0:
        data["has_coverage"] = True
        data["target_median"] = round(float(median_target), 2)
        data["target_mean"] = round(float(info.get("targetMeanPrice", median_target)), 2)
        data["target_high"] = round(float(info.get("targetHighPrice", median_target)), 2)
        data["target_low"] = round(float(info.get("targetLowPrice", median_target)), 2)
        data["analyst_count"] = int(analyst_count)
        data["recommendation"] = info.get("recommendationKey", "N/A").upper()

        if cmp and cmp > 0:
            upside = ((median_target - cmp) / cmp) * 100.0
            data["upside_pct"] = round(upside, 1)

    if info.get("forwardPE"):
        try:
            data["forward_pe"] = round(float(info["forwardPE"]), 1)
        except (ValueError, TypeError):
            pass

    if info.get("forwardEps"):
        try:
            data["forward_eps"] = round(float(info["forwardEps"]), 2)
        except (ValueError, TypeError):
            pass

    if info.get("beta"):
        try:
            data["beta"] = round(float(info["beta"]), 2)
        except (ValueError, TypeError):
            pass

    if info.get("enterpriseToEbitda"):
        try:
            data["ev_to_ebitda"] = round(float(info["enterpriseToEbitda"]), 1)
        except (ValueError, TypeError):
            pass

    if info.get("heldPercentInstitutions") is not None:
        try:
            data["institutional_holding_pct"] = round(float(info["heldPercentInstitutions"]) * 100.0, 1)
        except (ValueError, TypeError):
            pass

    # 1. EPS Revisions (Up vs Down in last 30 days)
    try:
        rev = t_obj.eps_revisions
        if rev is not None and not rev.empty:
            target_period = "+1y" if "+1y" in rev.index else ("0y" if "0y" in rev.index else None)
            if target_period:
                row = rev.loc[target_period]
                up30 = row.get("upLast30days")
                down30 = row.get("downLast30days")
                u_val = int(up30) if up30 == up30 and up30 is not None else 0
                d_val = int(down30) if down30 == down30 and down30 is not None else 0
                if u_val > 0 or d_val > 0:
                    data["eps_revisions_30d"] = f"{u_val} Up / {d_val} Down (30D)"
    except Exception as e:
        logger.debug(f"Error fetching eps_revisions for {clean_sym}: {e}")

    # 2. EPS Trend (Drift vs 30D / 90D)
    try:
        trend = t_obj.eps_trend
        if trend is not None and not trend.empty:
            target_period = "+1y" if "+1y" in trend.index else ("0y" if "0y" in trend.index else None)
            if target_period:
                row = trend.loc[target_period]
                curr = row.get("current")
                ago30 = row.get("30daysAgo")
                if curr and ago30 and curr == curr and ago30 == ago30 and ago30 > 0:
                    pct_drift = round(((curr - ago30) / ago30) * 100.0, 1)
                    sign = "+" if pct_drift > 0 else ""
                    data["eps_trend_30d"] = f"{sign}{pct_drift}% vs 30d ago (₹{curr:.2f})"
    except Exception as e:
        logger.debug(f"Error fetching eps_trend for {clean_sym}: {e}")

    # 3. Recent Ticker News (top 3 headlines)
    try:
        news_items = t_obj.news[:3] if t_obj.news else []
        headlines: List[str] = []
        for n in news_items:
            title = n.get("title")
            pub = n.get("publisher")
            if not title and isinstance(n.get("content"), dict):
                title = n["content"].get("title")
                pub = n["content"].get("provider", {}).get("displayName")
            if title:
                item_str = f"{title} ({pub})" if pub else title
                headlines.append(item_str)
        data["recent_news"] = headlines
    except Exception as e:
        logger.debug(f"Error fetching news for {clean_sym}: {e}")

    return data


if __name__ == "__main__":
    for test_s in ["RRKABEL", "POWERMECH", "BLS"]:
        res = fetch_brokerage_consensus(test_s)
        print(f"\n--- {test_s} ---")
        for k, v in res.items():
            print(f"  {k}: {v}")
