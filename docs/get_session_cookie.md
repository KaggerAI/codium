# How to Get Your Session Cookie

Your app requires authentication, so load tests need a valid session cookie. Here's how to get it:

## Method 1: Chrome/Edge DevTools (Recommended)

1. **Open your website** in Chrome/Edge
   - Go to `https://kagger.in`

2. **Login** to your account

3. **Open DevTools**
   - Press `F12` or `Ctrl+Shift+I`

4. **Go to Application tab**
   - Click "Application" in the top menu

5. **Find Cookies**
   - Left sidebar → Storage → Cookies → `https://kagger.in`

6. **Copy the session cookie**
   - Look for a cookie named `session` or similar
   - Copy the entire **Value** (the long random string)

7. **Format for the test**
   ```powershell
   # Use this format:
   python load_test.py --url https://kagger.in --requests 20 --concurrent 3 --session "session=YOUR_SESSION_VALUE"
   ```

## Method 2: Network Tab (Alternative)

1. **Open DevTools** (`F12`)

2. **Go to Network tab**

3. **Refresh the page** (`Ctrl+R`)

4. **Click any request** in the list

5. **Find Request Headers**
   - Scroll down to "Request Headers"
   - Look for `Cookie:` header
   - Copy the entire cookie string

6. **Use in test**
   ```powershell
   python load_test.py --url https://kagger.in --requests 20 --concurrent 3 --session "session=abc123..."
   ```

## Example Session Cookie

```
session=.eJwljk1qA0EMRe_idarL3a1fXYoXYUg8hCEl2SWLMLfPgHf6eJ_vXeOx7cf1dV7iEY-6xxvm6xKPuF_qPo_4dD--r-sej_EeM3ZoCGatoA4JbZlZtWqxHBJaQRu5qWpRXDO0_LZKpNRUSWZUqpVabCNKa5Rqc5LcpNTcvDYLdKkAAeAgFFJLqbkXq9ICPWBIK0gKZSDVD3T-AHn2N9I.Z3varg.1234567890abcdef
```

## Full Command Example

```powershell
# Install brotli first (if not done)
pip install brotli

# Run load test with authentication
python load_test.py --url https://kagger.in --requests 30 --concurrent 3 --session "session=.eJwljk1qA0EMRe_idarL3a1fXYoXYUg8hCEl2SWLMLfPgHf6eJ_vXeOx7cf1dV7iEY-6xxvm6xKPuF_qPo_4dD--r-sej_EeM3ZoCGatoA4JbZlZtWqxHBJaQRu5qWpRXDO0_LZKpNRUSWZUqpVabCNKa5Rqc5LcpNTcvDYLdKkAAeAgFFJLqbkXq9ICPWBIK0gKZSDVD3T-AHn2N9I.Z3varg.1234567890abcdef"
```

## Important Notes

⚠️ **Security Warning:**
- Don't share your session cookie with anyone - it gives full access to your account
- The session will expire after some time (check your Flask session timeout)
- If tests fail with 401/403, get a fresh session cookie

💡 **Tips:**
- The session cookie is usually named `session` in Flask apps
- Copy the ENTIRE value including any dots and special characters
- Wrap the session value in quotes when using in PowerShell
- If there are multiple cookies, you can pass them all: `--session "session=xxx; other_cookie=yyy"`

## Troubleshooting

**"Still getting HTTP 500 errors"**
- Cookie might have expired - get a fresh one
- Check if you copied the entire cookie value
- Try logging out and back in, then get a new cookie

**"401 Unauthorized"**
- Session cookie expired or invalid
- Get a fresh cookie from the browser

**"Other cookies needed?"**
- Some apps need CSRF tokens
- Copy ALL cookies from the Application tab
- Format: `--session "session=xxx; csrftoken=yyy"`
