with open("main.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

for idx, line in enumerate(lines):
    if "def view_" in line or 'self.tab == "utilities"' in line or 'self.tab == "tiện ích"' in line or 'self.tab == "tien_ich"' in line:
        print(f"Line {idx+1}: {line.strip()}")
