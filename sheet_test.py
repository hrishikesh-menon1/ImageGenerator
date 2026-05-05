import json, gspread
from google.oauth2.service_account import Credentials
with open('/Users/menon/dev/ImageGenerator/config.json', 'r') as f:
    config = json.load(f)
rules = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
login = Credentials.from_service_account_file(config["credentials_file"], scopes=rules)
gspread_app = gspread.authorize(login)
worksheet = gspread_app.open_by_key(config["sheet_id"]).sheet1
headers = worksheet.row_values(1)
print("Headers:", headers)
all_info = worksheet.get_all_records()
print("Total rows:", len(all_info))
if all_info:
    print("Sample row 1:", all_info[0])
