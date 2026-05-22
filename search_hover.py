with open("main.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

for idx, line in enumerate(lines):
    if "def make_hoverable_card" in line:
        print(f"Line {idx+1}: {line.strip()}")
        # print 20 lines around
        for j in range(max(0, idx-2), min(len(lines), idx+30)):
            print(f"  {j+1}: {lines[j]}", end="")
        break
