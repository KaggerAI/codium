```
    # create virtual environment to keep your libraries separate
    >python3 -m venv codeium_env

    # activate the virtual enviroment
    >source codeium_env/bin/activate

    # to deactivate
    >deactivate    

    # install dependencies [method 1]
    >pip install -r requirements.txt

    # install dependencies [method 2]
    >pip install flask flask-cors yfinance pandas numpy matplotlib openai yfinance plotly lxml
    >pip install --upgrade --no-cache-dir git+https://github.com/rongardF/tvdatafeed.git
    >pip install TA-lib

    # run the webserver
    >python3 handler.py
```