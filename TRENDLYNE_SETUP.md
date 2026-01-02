# Setting Up Trendlyne Credentials

## Local Development (Windows)

### Option 1: Using PowerShell (Temporary - lasts only for current session)
```powershell
$env:TRENDLYNE_USERNAME = "your_email@example.com"
$env:TRENDLYNE_PASSWORD = "your_password"
```

### Option 2: Using .env file (Recommended - Persistent)

1. **Create a `.env` file** in `c:\Users\harsh\codium\`:
   ```
   TRENDLYNE_USERNAME=your_email@example.com
   TRENDLYNE_PASSWORD=your_password
   ```

2. **Install python-dotenv** (if not already installed):
   ```powershell
   venv\Scripts\pip install python-dotenv
   ```

3. **Update handler.py** to load .env file (I'll do this automatically)

### Option 3: System Environment Variables (Persistent across all sessions)

1. Open Start Menu → Search "Environment Variables"
2. Click "Edit the system environment variables"
3. Click "Environment Variables" button
4. Under "User variables" click "New"
5. Add:
   - Variable name: `TRENDLYNE_USERNAME`
   - Variable value: `your_email@example.com`
6. Repeat for `TRENDLYNE_PASSWORD`
7. Restart your terminal/IDE

---

## Azure Deployment

### Adding Environment Variables in Azure App Service

1. Go to **Azure Portal** → Your App Service
2. Navigate to **Settings** → **Configuration**
3. Under **Application settings**, click **+ New application setting**
4. Add two settings:
   - **Name**: `TRENDLYNE_USERNAME`, **Value**: `your_email@example.com`
   - **Name**: `TRENDLYNE_PASSWORD`, **Value**: `your_password`
5. Click **Save** at the top
6. Click **Continue** to restart the app

### Security Note
- These credentials will be stored securely
- They won't appear in your code or version control
- They're encrypted at rest in Azure

---

## Verification

After setting up, restart your Flask server and check the endpoint:
```
http://localhost:8000/debug/env
```

It should show `TRENDLYNE_USERNAME` and `TRENDLYNE_PASSWORD` as `true`.

---

## My Recommendation

For local: **Option 2** (.env file) - cleanest and most flexible
For Azure: **Application Settings** (only option available)

Let me know when you've added the credentials, and I'll start implementing the PDF summary feature!
