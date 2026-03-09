import json
import os
import gspread
from google.oauth2.service_account import Credentials

def get_sheet():
    """
    透過環境變數中的 JSON 金鑰與 Sheet ID，回傳 Google Sheet 工作表實例。
    """
    # 從環境變數讀取金鑰 JSON
    service_account_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",  # 必要
        "https://www.googleapis.com/auth/drive"          # 可選（若有從 Drive 開啟 Sheets）
    ]
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)

    
    # 授權並取得工作表
    client = gspread.authorize(creds)
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    return client.open_by_key(sheet_id).sheet1  # 使用第1個工作表
