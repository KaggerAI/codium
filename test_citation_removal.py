import re

# Test the regex pattern with the user's sample text
sample_text = """The spike in Nifty volatility over the last two sessions appears driven by a mix of global risk-off cues (especially US–Venezuela tensions and trade rhetoric), profit‑booking after record highs, and FII selling, with sector‑specific pressure in IT and energy raising risk perception and near‑term volatility expectations.[2][5][9][14] Investor sentiment has shifted from "buy-the-dip" to more cautious positioning, while domestic flows are cushioning the downside, creating both short-term trading opportunities and higher drawdown risk.[2][8][15]"""

# Apply the regex substitution
cleaned_text = re.sub(r'\[\d+\]', '', sample_text)

print("Original text:")
print(sample_text)
print("\n" + "="*80 + "\n")
print("Cleaned text (citations removed):")
print(cleaned_text)
print("\n" + "="*80 + "\n")

# Verify that only the citation brackets are removed
assert "[2]" not in cleaned_text
assert "[5]" not in cleaned_text
assert "[9]" not in cleaned_text
assert "[14]" not in cleaned_text
assert "[15]" not in cleaned_text
assert "[8]" not in cleaned_text

# Verify the actual content is still intact
assert "Nifty volatility" in cleaned_text
assert "US–Venezuela tensions" in cleaned_text
assert "buy-the-dip" in cleaned_text

print("✅ All tests passed! Citation brackets successfully removed.")
