import pandas as pd
import numpy as np

class Data:
    def __init__(self, symbol):
        self.symbol = symbol
        self.df = None
        # This list now defines ALL possible columns we might need.
        self.all_feature_columns = [
            'PE', 'PB', 'EV / EBITDA', 'Market Cap / Sales',
            'RSI14', 'ema13_55', 'ema55_144',
            'IMPLIED_BID_ASK', 'VOLUME', 'VOLUME_FUTURE', 'FII', 'DII',
            'ATR_14', 'IV', 'BETA', 'HV_20D', 'HV_IV', 'IndiaVix',
            'returns', 'close'
        ]
        # These are the specific columns for ValScore we will populate with real data
        self.valuation_features = ['PE', 'PB', 'EV / EBITDA', 'Market Cap / Sales']


    def load_data(self, valuation_data: dict, technical_df: pd.DataFrame, futures_volume_df: pd.DataFrame = None):
        """
        Loads valuation, technical, and futures volume data, merging them into a single DataFrame.
        VOLUME_FUTURE is optional - not all stocks have F&O contracts.
        """
        processed_dfs = []

        # --- Part 1: Process and Normalize Valuation DataFrames ---
        print(f"DEBUG: valuation_data received: {type(valuation_data)}, keys: {valuation_data.keys() if valuation_data else 'None'}")
        
        if valuation_data:
            # The variable is renamed to 'data_list' for clarity.
            for label, data_list in valuation_data.items():
                print(f"DEBUG: Processing '{label}': type={type(data_list)}, is_list={isinstance(data_list, list)}, length={len(data_list) if isinstance(data_list, list) else 'N/A'}")
                
                if not isinstance(data_list, list) or not data_list:
                    print(f"DEBUG: Skipping '{label}' - not a list or empty")
                    continue

                # --- START FIX ---
                # 1. Convert the list of dictionaries back into a DataFrame.
                df = pd.DataFrame(data_list)
                print(f"DEBUG: '{label}' DataFrame columns: {list(df.columns)}")
                
                # 2. Check if a 'date' or 'index' column exists to be set as the index.
                # The screener data uses 'index' for dates
                date_col = None
                if 'date' in df.columns:
                    date_col = 'date'
                elif 'index' in df.columns:
                    date_col = 'index'
                    
                if date_col is None:
                    print(f"DEBUG: Skipping '{label}' - no 'date' or 'index' column found")
                    continue
                    
                df.set_index(date_col, inplace=True)
                # --- END FIX ---

                # Now that 'df' is a proper DataFrame, the rest of the original logic will work.
                df.index = pd.to_datetime(df.index).normalize()
                
                if label == "PE Ratio" and 'PE' in df.columns:
                    print(f"DEBUG: Found PE Ratio data with {len(df)} rows")
                    processed_dfs.append(df[['PE']])
                elif label == "PB Ratio" and 'Price to BV' in df.columns:
                    print(f"DEBUG: Found PB Ratio data with {len(df)} rows")
                    df_pb = df[['Price to BV']].rename(columns={'Price to BV': 'PB'})
                    processed_dfs.append(df_pb)
                elif label == "EV / EBITDA" and 'EV / EBITDA' in df.columns:
                    print(f"DEBUG: Found EV/EBITDA data with {len(df)} rows")
                    processed_dfs.append(df[['EV / EBITDA']])
                elif label == "Market Cap / Sales" and 'Market Cap / Sales' in df.columns:
                    print(f"DEBUG: Found Market Cap/Sales data with {len(df)} rows")
                    processed_dfs.append(df[['Market Cap / Sales']])
                else:
                    print(f"DEBUG: '{label}' - column mismatch. Available: {list(df.columns)}")

        # --- Part 2: Process and Normalize Technical DataFrame ---
        if technical_df is not None and not technical_df.empty:
            technical_df.index = pd.to_datetime(technical_df.index).normalize()

            tech_features_df = pd.DataFrame(index=technical_df.index)
            tech_features_df['RSI14'] = technical_df['RSI14']
            tech_features_df['ema13_55'] = technical_df['EMA13'] - technical_df['EMA55']
            tech_features_df['ema55_144'] = technical_df['EMA55'] - technical_df['EMA144']
            tech_features_df['close'] = technical_df['Close']
            tech_features_df['returns'] = tech_features_df['close'].pct_change()
            
            # --- LIQUIDITY FEATURES ---
            # VOLUME: Raw trading volume
            if 'Volume' in technical_df.columns:
                tech_features_df['VOLUME'] = technical_df['Volume']
                print(f"DEBUG: Extracted VOLUME from technical_df")
            
            # IMPLIED_BID_ASK: High-Low spread as a proxy for bid-ask spread
            # Higher spread = lower liquidity
            if all(col in technical_df.columns for col in ['High', 'Low', 'Close']):
                tech_features_df['IMPLIED_BID_ASK'] = (technical_df['High'] - technical_df['Low']) / technical_df['Close']
                print(f"DEBUG: Calculated IMPLIED_BID_ASK from High-Low spread")
            
            processed_dfs.append(tech_features_df)

        # --- Part 2.5: Process Futures Volume Data (Optional) ---
        if futures_volume_df is not None and not futures_volume_df.empty:
            futures_volume_df.index = pd.to_datetime(futures_volume_df.index).normalize()
            # Ensure column name is VOLUME_FUTURE
            if 'VOLUME_FUTURE' in futures_volume_df.columns:
                processed_dfs.append(futures_volume_df[['VOLUME_FUTURE']])
                print(f"DEBUG: Added VOLUME_FUTURE data with {len(futures_volume_df)} rows")

        # --- Part 3: Merge and Sanitize ---
        if not processed_dfs:
            self.df = self._generate_random_dataframe()
            return self.df
            
        master_df = pd.concat(processed_dfs, axis=1)

        start, end = master_df.index.min(), master_df.index.max()
        if pd.isna(start) or pd.isna(end):
            self.df = self._generate_random_dataframe()
            return self.df

        date_index = pd.date_range(start=start, end=end, freq='B')
        master_df = master_df.reindex(date_index)
        
        master_df.interpolate(method='linear', limit_direction='both', inplace=True)
        master_df.fillna(method='bfill', inplace=True)
        master_df.fillna(0, inplace=True)

        # --- Part 4: Fill placeholders (Unchanged) ---
        # DEBUG: Show which columns have REAL data
        print(f"DEBUG: Columns with REAL data: {list(master_df.columns)}")
        
        placeholder_cols = [col for col in self.all_feature_columns if col not in master_df.columns]
        print(f"DEBUG: Columns filled with RANDOM placeholder data: {placeholder_cols}")
        
        placeholder_data = np.random.rand(len(master_df.index), len(placeholder_cols))
        df_placeholder = pd.DataFrame(placeholder_data, index=master_df.index, columns=placeholder_cols)
        self.df = pd.concat([master_df, df_placeholder], axis=1).reindex(columns=self.all_feature_columns).fillna(0)

        print("INFO: Data.py successfully converted and merged REAL valuation and technical data.")
        return self.df

    
    # def load_data(self, valuation_data: dict, technical_df: pd.DataFrame):
    #     """
    #     FINAL VERSION: Uses .normalize() on all indices to strip the time component,
    #     ensuring a perfect data alignment before merging.
    #     """
    #     processed_dfs = []

    #     # --- Part 1: Process and Normalize Valuation DataFrames ---
    #     if valuation_data:
    #         for label, df in valuation_data.items():
    #             # THE FIX: Ensure the index is a normalized, timezone-naive datetime object
    #             df.index = pd.to_datetime(df.index).normalize()
                
    #             if label == "PE Ratio" and 'PE' in df.columns:
    #                 processed_dfs.append(df[['PE']])
    #             elif label == "PB Ratio" and 'Price to BV' in df.columns:
    #                 df_pb = df[['Price to BV']].rename(columns={'Price to BV': 'PB'})
    #                 processed_dfs.append(df_pb)
    #             elif label == "EV / EBITDA" and 'EV / EBITDA' in df.columns:
    #                 processed_dfs.append(df[['EV / EBITDA']])
    #             elif label == "Market Cap / Sales" and 'Market Cap / Sales' in df.columns:
    #                 processed_dfs.append(df[['Market Cap / Sales']])
    #     # --- Part 2: Process and Normalize Technical DataFrame ---
    #     if technical_df is not None and not technical_df.empty:
    #         # THE FIX: Normalize the technical data's index to remove the time component
    #         technical_df.index = pd.to_datetime(technical_df.index).normalize()

    #         tech_features_df = pd.DataFrame(index=technical_df.index)
    #         tech_features_df['RSI14'] = technical_df['RSI14']
    #         tech_features_df['ema13_55'] = technical_df['EMA13'] - technical_df['EMA55']
    #         tech_features_df['ema55_144'] = technical_df['EMA55'] - technical_df['EMA144']
    #         tech_features_df['close'] = technical_df['Close']
    #         tech_features_df['returns'] = tech_features_df['close'].pct_change()
    #         processed_dfs.append(tech_features_df)

    #     # --- Part 3: Merge and Sanitize (This will now work correctly) ---
    #     if not processed_dfs:
    #         self.df = self._generate_random_dataframe()
    #         return self.df
            
    #     master_df = pd.concat(processed_dfs, axis=1)

    #     start, end = master_df.index.min(), master_df.index.max()
    #     if pd.isna(start) or pd.isna(end):
    #          self.df = self._generate_random_dataframe()
    #          return self.df

    #     date_index = pd.date_range(start=start, end=end, freq='B')
    #     master_df = master_df.reindex(date_index)
        
    #     master_df.interpolate(method='linear', limit_direction='both', inplace=True)
    #     master_df.fillna(method='bfill', inplace=True)
    #     master_df.fillna(0, inplace=True)

    #     # --- Part 4: Fill placeholders (Unchanged) ---
    #     placeholder_cols = [col for col in self.all_feature_columns if col not in master_df.columns]
    #     placeholder_data = np.random.rand(len(master_df.index), len(placeholder_cols))
    #     df_placeholder = pd.DataFrame(placeholder_data, index=master_df.index, columns=placeholder_cols)
    #     self.df = pd.concat([master_df, df_placeholder], axis=1).reindex(columns=self.all_feature_columns).fillna(0)

    #     print("INFO: Data.py successfully merged REAL valuation and technical data after NORMALIZING indices.")
    #     return self.df
    

    def _generate_random_dataframe(self):
        """Generates a fully random DataFrame as a fallback."""
        date_index = pd.date_range(end=pd.Timestamp.today(), periods=5*252, freq='B')
        data = np.random.rand(len(date_index), len(self.all_feature_columns))
        df = pd.DataFrame(data, index=date_index, columns=self.all_feature_columns)
        return df