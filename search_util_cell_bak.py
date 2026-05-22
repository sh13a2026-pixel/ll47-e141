with open("main.py.bak", "r", encoding="utf-8") as f:
    lines = f.readlines()

for idx, line in enumerate(lines):
    if "def view_utilities" in line or "util_cell" in line:
        print(f"Line {idx+1}: {line.strip()}")
        # print 50 lines around
        for j in range(max(0, idx-2), min(len(lines), idx+60)):
            print(f"  {j+1}: {lines[j]}", end="")
        break
