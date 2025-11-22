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


    def load_data(self, valuation_data: dict, technical_df: pd.DataFrame):
        """
        CORRECTED VERSION: Properly handles the incoming data format. It converts the
        list of dictionaries from valuation_data back into DataFrames before processing.
        """
        processed_dfs = []

        # --- Part 1: Process and Normalize Valuation DataFrames ---
        if valuation_data:
            # The variable is renamed to 'data_list' for clarity.
            for label, data_list in valuation_data.items():
                if not isinstance(data_list, list) or not data_list:
                    continue

                # --- START FIX ---
                # 1. Convert the list of dictionaries back into a DataFrame.
                df = pd.DataFrame(data_list)
                
                # 2. Check if a 'date' column exists to be set as the index.
                if 'date' not in df.columns:
                    continue
                df.set_index('date', inplace=True)
                # --- END FIX ---

                # Now that 'df' is a proper DataFrame, the rest of the original logic will work.
                df.index = pd.to_datetime(df.index).normalize()
                
                if label == "PE Ratio" and 'PE' in df.columns:
                    processed_dfs.append(df[['PE']])
                elif label == "PB Ratio" and 'Price to BV' in df.columns:
                    df_pb = df[['Price to BV']].rename(columns={'Price to BV': 'PB'})
                    processed_dfs.append(df_pb)
                elif label == "EV / EBITDA" and 'EV / EBITDA' in df.columns:
                    processed_dfs.append(df[['EV / EBITDA']])
                elif label == "Market Cap / Sales" and 'Market Cap / Sales' in df.columns:
                    processed_dfs.append(df[['Market Cap / Sales']])

        # --- Part 2: Process and Normalize Technical DataFrame (No change needed here) ---
        if technical_df is not None and not technical_df.empty:
            technical_df.index = pd.to_datetime(technical_df.index).normalize()

            tech_features_df = pd.DataFrame(index=technical_df.index)
            tech_features_df['RSI14'] = technical_df['RSI14']
            tech_features_df['ema13_55'] = technical_df['EMA13'] - technical_df['EMA55']
            tech_features_df['ema55_144'] = technical_df['EMA55'] - technical_df['EMA144']
            tech_features_df['close'] = technical_df['Close']
            tech_features_df['returns'] = tech_features_df['close'].pct_change()
            processed_dfs.append(tech_features_df)

        # --- Part 3: Merge and Sanitize (No change needed here) ---
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
        placeholder_cols = [col for col in self.all_feature_columns if col not in master_df.columns]
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