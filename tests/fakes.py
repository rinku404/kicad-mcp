"""Canned Mouser / DigiKey API responses, shaped like the real ones."""

MOUSER_PART = {
    "MouserPartNumber": "595-LM358DR",
    "ManufacturerPartNumber": "LM358DR",
    "Manufacturer": "Texas Instruments",
    "Description": "Operational Amplifiers - Op Amps Dual Gen Purpose",
    "DataSheetUrl": "{datasheet}",
    "ProductDetailUrl": "https://www.mouser.com/ProductDetail/595-LM358DR",
    "AvailabilityInStock": "12345",
    "LifecycleStatus": None,
    "Min": "1",
    "PriceBreaks": [
        {"Quantity": 1, "Price": "$0.50", "Currency": "USD"},
        {"Quantity": 10, "Price": "$0.35", "Currency": "USD"},
        {"Quantity": 100, "Price": "$0.20", "Currency": "USD"},
    ],
    "ProductAttributes": [{"AttributeName": "Package / Case", "AttributeValue": "SOIC-8"}],
}

DIGIKEY_PRODUCT = {
    "ManufacturerProductNumber": "LM358DR",
    "Manufacturer": {"Name": "Texas Instruments"},
    "Description": {"ProductDescription": "IC OPAMP GP 2 CIRCUIT 8SOIC"},
    "DatasheetUrl": "{datasheet}",
    "ProductUrl": "https://www.digikey.com/en/products/detail/texas-instruments/LM358DR/555",
    "QuantityAvailable": 50000,
    "ProductStatus": {"Status": "Active"},
    "Parameters": [{"ParameterText": "Package / Case", "ValueText": "8-SOIC"}],
    "ProductVariations": [
        {
            "DigiKeyProductNumber": "296-1395-1-ND",
            "PackageType": {"Name": "Cut Tape (CT)"},
            "MinimumOrderQuantity": 1,
            "StandardPricing": [
                {"BreakQuantity": 1, "UnitPrice": 0.45},
                {"BreakQuantity": 10, "UnitPrice": 0.30},
            ],
        },
        {
            "DigiKeyProductNumber": "296-1395-2-ND",
            "PackageType": {"Name": "Tape & Reel (TR)"},
            "MinimumOrderQuantity": 2500,
            "StandardPricing": [{"BreakQuantity": 2500, "UnitPrice": 0.10}],
        },
    ],
}


def install(monkeypatch, datasheet_url="https://example.invalid/lm358.pdf"):
    """Route the distributor clients' HTTP calls to canned data. Returns the list of calls made."""
    import copy

    from kicad_mcp import distributors

    calls = []

    def fake(url, *, data=None, form=None, headers=None):
        calls.append(url)
        mp = copy.deepcopy(MOUSER_PART)
        mp["DataSheetUrl"] = datasheet_url
        dk = copy.deepcopy(DIGIKEY_PRODUCT)
        dk["DatasheetUrl"] = datasheet_url
        if "mouser.com" in url:
            return {"Errors": [], "SearchResults": {"NumberOfResult": 1, "Parts": [mp]}}
        if url.endswith("/oauth2/token"):
            return {"access_token": "t", "expires_in": 600}
        if url.endswith("/productdetails"):
            return {"Product": dk}
        if "digikey.com" in url:
            return {"Products": [dk], "ExactMatches": []}
        raise AssertionError(url)

    monkeypatch.setattr(distributors, "_http_json", fake)
    monkeypatch.setenv("MOUSER_API_KEY", "k")
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    return calls
