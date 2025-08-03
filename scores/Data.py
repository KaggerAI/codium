import pandas as pd
import numpy as np

class Data:
    def __init__(self, symbol):
        self.symbol = symbol
        self.df = None
        # This list now defines ALL possible columns we might need.
        self.all_feature_columns = [
            'PE', 'PB', 'EBITDA_BY_EV', 'SALES_BY_MCAP',
            'RSI14', 'ema13_55', 'ema55_144',
            'IMPLIED_BID_ASK', 'VOLUME', 'VOLUME_FUTURE', 'FII', 'DII',
            'ATR_14', 'IV', 'BETA', 'HV_20D', 'HV_IV', 'IndiaVix',
            'returns', 'close'
        ]
        # These are the specific columns for ValScore we will populate with real data
        self.valuation_features = ['PE', 'PB', 'EBITDA_BY_EV', 'SALES_BY_MCAP']

    def load_data(self, valuation_data: dict):
        """
        Loads and processes real valuation data, filling other features with placeholders.
        
        Args:
            valuation_data (dict): The 'valuation_and_margin_data' section from last_analysis.
        """
        processed_dfs = []

        # --- Process each valuation metric from the input data ---
        
        # 1. Process PE Ratio
        pe_series_data = valuation_data.get("PE Ratio", [])
        if pe_series_data:
            df_pe = pd.DataFrame(pe_series_data).set_index('date')
            # FIX 1: Explicitly convert to numeric, turning errors into NaN
            df_pe['PE'] = pd.to_numeric(df_pe['PE'], errors='coerce')
            processed_dfs.append(df_pe[['PE']])

        # 2. Process PB Ratio
        pb_series_data = valuation_data.get("PB Ratio", [])
        if pb_series_data:
            df_pb = pd.DataFrame(pb_series_data).set_index('date')
            df_pb.rename(columns={'Price to BV': 'PB'}, inplace=True)
            df_pb['PB'] = pd.to_numeric(df_pb['PB'], errors='coerce')
            processed_dfs.append(df_pb[['PB']])
            
        # 3. Process EV / EBITDA (and calculate its inverse)
        ev_ebitda_series_data = valuation_data.get("EV / EBITDA", [])
        if ev_ebitda_series_data:
            df_ev_ebitda = pd.DataFrame(ev_ebitda_series_data).set_index('date')
            df_ev_ebitda['EV / EBITDA'] = pd.to_numeric(df_ev_ebitda['EV / EBITDA'], errors='coerce')
            # FIX 2: Safely calculate inverse, avoiding division by zero
            df_ev_ebitda['EBITDA_BY_EV'] = 1 / df_ev_ebitda['EV / EBITDA'].replace(0, np.nan)
            processed_dfs.append(df_ev_ebitda[['EBITDA_BY_EV']])

        # 4. Process Market Cap / Sales (and calculate its inverse)
        mcap_sales_series_data = valuation_data.get("Market Cap / Sales", [])
        if mcap_sales_series_data:
            df_mcap_sales = pd.DataFrame(mcap_sales_series_data).set_index('date')
            df_mcap_sales['Market Cap / Sales'] = pd.to_numeric(df_mcap_sales['Market Cap / Sales'], errors='coerce')
            df_mcap_sales['SALES_BY_MCAP'] = 1 / df_mcap_sales['Market Cap / Sales'].replace(0, np.nan)
            processed_dfs.append(df_mcap_sales[['SALES_BY_MCAP']])

        # --- Combine all processed data and create the master DataFrame ---
        if not processed_dfs:
            print("WARNING: No valuation data found. Falling back to fully random data.")
            self.df = self._generate_random_dataframe()
            return self.df

        master_df = pd.concat(processed_dfs, axis=1)
        master_df.index = pd.to_datetime(master_df.index)

        date_index = pd.date_range(start=master_df.index.min(), end=master_df.index.max(), freq='B')
        master_df = master_df.reindex(date_index)

        # FIX 3: Robustly fill gaps. First with the mean, then with 0 for any columns that were entirely NaN.
        master_df.fillna(master_df.mean(), inplace=True)
        master_df.fillna(0, inplace=True)

        # --- Fill placeholder data for all other required features ---
        placeholder_cols = [col for col in self.all_feature_columns if col not in master_df.columns]
        
        placeholder_data = np.random.rand(len(master_df.index), len(placeholder_cols))
        df_placeholder = pd.DataFrame(placeholder_data, index=master_df.index, columns=placeholder_cols)

        self.df = pd.concat([master_df, df_placeholder], axis=1)
        
        for col in self.all_feature_columns:
            if col not in self.df.columns:
                 self.df[col] = 0 # Fill any completely missing columns with 0

        print("INFO: Data.py loaded with REAL valuation data and PLACEHOLDER data for other scores.")
        return self.df[self.all_feature_columns]

    def _generate_random_dataframe(self):
        """Generates a fully random DataFrame as a fallback."""
        date_index = pd.date_range(end=pd.Timestamp.today(), periods=5*252, freq='B')
        data = np.random.rand(len(date_index), len(self.all_feature_columns))
        df = pd.DataFrame(data, index=date_index, columns=self.all_feature_columns)
        return df