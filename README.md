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
    >pip install google-generativeai
    # rest use requriements.txt

    # for perplexityai dependencies
    > pip install git+https://github.com/nathanrchn/perplexityai.git
    or
    > https://github.com/nathanrchn/perplexityai
    > cd perplexity; pip install -e .
    # bugifix see the pull requests in github 

    # need .env file
    # OPENAI_API_KEY = ""
    # PERPLEXITY_API_KEY = ""
    # GOOGLE_API_KEY = ""

    # run the webserver
    >python3 handler.py
```