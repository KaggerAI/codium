import plotly.graph_objects as go
import plotly.express as px

class PlotScore:

    @staticmethod
    def multiple_lines(df, columns, category_column=None, regime_labels=None):
        # Define the colors (using Plotly color palette)
        colors = px.colors.qualitative.Plotly
        # Map the regime labels to colors
        color_map = {regime: colors[i % len(colors)] for i, regime in enumerate(regime_labels.keys())}
        
        # Initialize the plot
        fig = go.Figure()

        # Loop through each column to plot the lines
        for col in columns:
            current_zone = df[category_column].iloc[0]
            segment_x = [df.index[0]]
            segment_y = [df[col].iloc[0]]

            for i in range(1, len(df)):
                zone = df[category_column].iloc[i]
                if zone == current_zone:
                    segment_x.append(df.index[i])
                    segment_y.append(df[col].iloc[i])
                else:
                    # Add the previous segment with a trace for the line plot
                    fig.add_trace(go.Scatter(
                        x=segment_x,
                        y=segment_y,
                        mode='lines',
                        name=f"{col} ({regime_labels.get(current_zone, 'Unknown')})",
                        line=dict(color=color_map.get(current_zone, 'black')),
                        showlegend=False
                    ))
                    current_zone = zone
                    segment_x = [df.index[i-1], df.index[i]]
                    segment_y = [df[col].iloc[i-1], df[col].iloc[i]]

            # Add the last segment
            fig.add_trace(go.Scatter(
                x=segment_x,
                y=segment_y,
                mode='lines',
                name=f"{col} ({regime_labels.get(current_zone, 'Unknown')})",
                line=dict(color=color_map.get(current_zone, 'black')),
                showlegend=False
            ))

        # Add a legend for the regimes based on string labels
        for regime, color in color_map.items():
            fig.add_trace(go.Scatter(
                x=[None], y=[None],
                mode='lines',
                line=dict(color=color),
                name=f"{regime_labels.get(regime, 'Unknown')}"
            ))

        # Add text annotations to identify regimes on the plot
        for regime, color in color_map.items():
            fig.add_trace(go.Scatter(
                x=[None], y=[None],
                mode='text',
                text=[f'{regime_labels.get(regime, "Unknown")}'],
                textposition="bottom right",
                showlegend=False
            ))

        # Update plot layout
        fig.update_layout(title="Line Plot by Regime", xaxis_title="datetime (index)", showlegend=True)
        fig.show()