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
        return fig

    @staticmethod
    def plot_violin(df, x_col, y_col, color_col=None, title=None, box=True, points='all'):
        """
        Plots a violin plot using Plotly.

        Parameters:
            df : pandas.DataFrame
                DataFrame containing the data.
            x_col : str
                Column name to use on the x-axis (categorical/grouping).
            y_col : str
                Column name to use on the y-axis (numeric value to plot).
            color_col : str or None
                Column to use for coloring (optional).
            title : str or None
                Plot title.
            box : bool
                Show inner boxplot.
            points : str or bool
                Show all data points ('all'), only outliers ('outliers'), or False.

        Returns:
            plotly.graph_objs._figure.Figure
        """
        df[x_col] = df[x_col].astype(str)
        fig = px.violin(
            df,
            x=x_col,
            y=y_col,
            color=color_col if color_col else x_col,
            box=box,
            points=points,
            title=title or f"Violin plot of {y_col} by {x_col}"
        )
        fig.update_layout(
            xaxis_title=x_col,
            yaxis_title=y_col,
            legend_title=color_col if color_col else x_col
        )
        return fig

    @staticmethod
    def plot_radar(data_dicts, title="RadarChart"):
        if isinstance(data_dicts, dict):
            data_dicts = [data_dicts]

        # Union of all categories, preserving order
        categories = list(data_dicts[0].keys())
        for d in data_dicts[1:]:
            for k in d.keys():
                if k not in categories:
                    categories.append(k)

        fig = go.Figure()
        descriptions_to_display = []
        for i, d in enumerate(data_dicts):
            # For missing categories, use (0, "")
            r = []
            for cat in categories:
                v, desc = d.get(cat, (0, ""))
                r.append(v)
                if desc:
                    descriptions_to_display.append(f"{cat}: {desc}")
            # Radar needs to close the shape
            r.append(r[0])
            theta = categories + [categories[0]]
            name = getattr(d, 'name', f"RadarScores")
            fig.add_trace(go.Scatterpolar(
                r=r,
                theta=theta,
                fill='toself',
                name=name
            ))

        fig.update_layout(
            polar=dict(radialaxis=dict(visible=True, range=[0, 1])),
            showlegend=True,
            title=title
        )

        # Show descriptions (if any) as annotation at bottom of plot
        if descriptions_to_display:
            annotation_text = "<br>".join(descriptions_to_display)
            fig.add_annotation(
                x=0, y=0,
                xref="paper", yref="paper",
                text=f"{annotation_text}",
                showarrow=False,
                align="left",
                font=dict(size=13),
                bordercolor="#c7c7c7",
                borderwidth=1,
                borderpad=4,
                bgcolor="#fafafa",
                opacity=0.8
            )

        #fig.show()
        return fig
