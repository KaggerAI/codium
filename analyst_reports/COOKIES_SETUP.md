# How to Export Cookies from Your Browser

## Option 1: Using Browser DevTools (Manual)

1. **Open Trendlyne** in your browser and **log in**
2. **Open DevTools** (F12)
3. Go to **Application** tab (Chrome) or **Storage** tab (Firefox)
4. Click **Cookies** → **https://trendlyne.com**
5. Look for important cookies like:
   - `sessionid`
   - `csrftoken`
   - `_ga`, `_gid` (analytics)
   
6. **Create this file**: `c:\Users\harsh\codium\analyst_reports\trendlyne_cookies.json`

7. **Format** (example):
   ```json
   {
     "sessionid": "your_session_id_value_here",
     "csrftoken": "your_csrf_token_here",
     "_ga": "GA1.2.xxxxx",
     "_gid": "GA1.2.xxxxx"
   }
   ```

## Option 2: Using Browser Extension (Easier)

1. **Install Extension**:
   - Chrome: [Cookie-Editor](https://chrome.google.com/webstore/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)
   - Firefox: [Cookie Quick Manager](https://addons.mozilla.org/en-US/firefox/addon/cookie-quick-manager/)

2. **Export Cookies**:
   - Go to https://trendlyne.com (while logged in)
   - Click the extension icon
   - Click "Export" → Choose "JSON" format
   - Save as `trendlyne_cookies.json` in `c:\Users\harsh\codium\analyst_reports\`

3. **You may need to convert** the format to simple key-value:
   ```json
   {
     "sessionid": "value1",
     "csrftoken": "value2"
   }
   ```

## Testing

After creating the cookies file:
```powershell
cd c:\Users\harsh\codium
venv\Scripts\python analyst_reports\pdf_summarizer_cookies.py
```

## Note

- Cookies typically expire after **2-4 weeks**
- You'll need to re-export them when they expire
- The error message will tell you when cookies are expired

## Updating handler.py

I'll update the code to use this cookie-based approach automatically.
