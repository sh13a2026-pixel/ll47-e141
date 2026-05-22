with open("main.py", "r", encoding="utf-8") as f:
    lines = f.readlines()
lines[803] = '            greet_main = f"Th.úy {profile.get(\'name\') or \'Không tên\'} 🫡"\n'
lines[811] = '                    ft.Text(f"{dow_label()}   {profile.get(\'unitName\') or \'\'}",\n'
with open("main.py", "w", encoding="utf-8") as f:
    f.writelines(lines)
