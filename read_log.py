
def print_debug_lines(f):
    for line in f:
        if "DEBUG:" in line:
            if "Screener Peer Table Columns Found" in line:
                print("FOUND COLUMNS LINE:")
                if "DEBUG:" in line:
                    if "Screener Peer Table Columns Found" in line:
                        print(f"Has 'BV' or 'Book': {'BV' in line or 'Book' in line}")
                        print(f"Has 'ROE': {'ROE' in line}")
                        print(f"Has 'Profit': {'Profit' in line}")
                        # Print substrings around matches
                        if 'BV' in line:
                            idx = line.find('BV')
                            print(f"Around BV: {line[max(0, idx-20):min(len(line), idx+20)]}")
                        if 'ROE' in line:
                            idx = line.find('ROE')
                            print(f"Around ROE: {line[max(0, idx-20):min(len(line), idx+20)]}")
                        elif 'roe' in line.lower():
                             print("Found lowercase roe")
                    else:
                        print(line.strip())

try:
    with open('combined_log.txt', 'r', encoding='utf-16le') as f:
        print_debug_lines(f)
except Exception:
    try:
        with open('combined_log.txt', 'r', encoding='utf-8') as f:
            print_debug_lines(f)
    except Exception as e:
        print(f"Error reading file: {e}")
