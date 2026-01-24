import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import csv
import os
import MetaTrader5 as mt5
from deepticks import TickBasedLadderStrategy

def optimize_parameters():
    """Run optimization for different parameter sets and export results to CSV"""
    
    SYMBOL = "GER40"
    ranges = {
        'entry_distance': [i for i in range(10, 100, 10)] + [i for i in range(100, 301, 20)],
        'tp_distance': [i for i in range(10, 50, 10)] + [i for i in range(50, 301, 50)],
        'sl_distance': range(10, 101, 10),
        'lot_size': [0.01],
        'steps': [1],
    }

    parameter_sets = generate_optimization_config(ranges)
    for p in parameter_sets:
        print(p)
    return
    cumulative_results = []

    start_date = datetime(2025, 1, 1)
    end_date = datetime(2026, 1, 24) # End date not included

    date_ranges = [(start_date, end_date)]

    if end_date - start_date >= timedelta(days=58):
        days_per_month = 30.44
        days_30 = timedelta(days=30)
        n_months = int((end_date - start_date).days // days_per_month)
        rem = (end_date - start_date).days % days_per_month
        rem_days = timedelta(days=rem)
        date_ranges = [(start_date + days_30 * i , start_date + days_30 * (i + 1)) for i in range(n_months)]
        date_ranges += [((start_date + (days_30 * (n_months - 1))), (start_date + rem_days  ))]

    
    for i, (start_date, end_date) in enumerate(date_ranges):
        print(f"Begining tests for month {i} [{start_date.isoformat()} - {end_date.isoformat()}]")
        skips = [0, 1, ]
		
        if i in skips:
            print("Skipping month...")
            continue
        # Initialize results list with more comprehensive metrics
        results = []
        
        strategy = TickBasedLadderStrategy(SYMBOL, 100)
        if strategy.initialize_mt5():
            # Get data
            tick_data = strategy.get_tick_data_range(
                start_date,
                end_date
            )

            if not tick_data:
                print(f"Failed to get data")
                continue
            
            for n, params in enumerate(parameter_sets):
                # print(f"\nTesting parameters: Entry=${params['entry_distance']}, TP=${params['tp_distance']}, Steps={params['steps']}")
                if n % 5 == 0:
                    print(f"{n} / {len(parameter_sets)}")
                
                # Reset strategy for each parameter set
                strategy = TickBasedLadderStrategy(SYMBOL, 100)
                strategy.entry_distance = params['entry_distance']
                strategy.tp_distance = params['tp_distance']
                strategy.sl_distance = params['sl_distance']
                strategy.lot_size = params['lot_size']
                strategy.num_steps = params['steps']
                
                if tick_data:
                    strategy.run_tick_backtest(tick_data)
                    
                    # Calculate key metrics
                    total_trades = strategy.total_trades
                    winning_trades = strategy.winning_trades
                    losing_trades = total_trades - winning_trades if total_trades > 0 else 0
                    
                    # Calculate profit/loss sums
                    positive_pnl = sum([p.pnl for p in strategy.closed_positions if p.pnl > 0])
                    negative_pnl = abs(sum([p.pnl for p in strategy.closed_positions if p.pnl <= 0]))

                    phantom_pnls = [p.phantom_pnl for p in strategy.closed_positions if p.phantom]

                    total_phantom_pnl = sum(phantom_pnls)
                    avg_phantom_pnl = np.mean(phantom_pnls)
                    
                    # Avoid division by zero
                    profit_factor = positive_pnl / negative_pnl if negative_pnl > 0 else 0
                    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
                    
                    # Calculate average win/loss
                    avg_win = positive_pnl / winning_trades if winning_trades > 0 else 0
                    avg_loss = negative_pnl / losing_trades if losing_trades > 0 else 0
                    
                    # Calculate expectancy
                    expectancy = (win_rate/100 * avg_win) - ((100-win_rate)/100 * avg_loss) if total_trades > 0 else 0
                    
                    # Calculate max drawdown (simplified - you might want to implement proper drawdown calculation)
                    cumulative_pnl = 0
                    max_drawdown = 0
                    peak = 0
                    
                    # Sort positions by close time if available
                    closed_positions_sorted = sorted(strategy.closed_positions, 
                                                    key=lambda x: x.close_time if hasattr(x, 'close_time') else 0)
                    
                    for pos in closed_positions_sorted:
                        cumulative_pnl += pos.pnl
                        if cumulative_pnl > peak:
                            peak = cumulative_pnl
                        drawdown = peak - cumulative_pnl
                        if drawdown > max_drawdown:
                            max_drawdown = drawdown
                    
                    results.append({
                        # Parameters
                        'entry_distance': params['entry_distance'],
                        'tp_distance': params['tp_distance'],
                        'sl_distance': params['sl_distance'],
                        'lot_size': params['lot_size'],
                        'steps': params['steps'],
                        
                        # Performance metrics
                        'total_pnl': strategy.total_pnl,
                        'total_trades': total_trades,
                        'winning_trades': winning_trades,
                        'losing_trades': losing_trades,
                        'win_rate': win_rate,
                        'profit_factor': profit_factor,
                        'early_tps': strategy.early_tps,
                        'total_phantom_pnl': total_phantom_pnl,
                        'avg_phantom_pnl': avg_phantom_pnl,
                        
                        # Advanced metrics
                        'avg_win': avg_win,
                        'avg_loss': avg_loss,
                        'expectancy': expectancy,
                        'max_drawdown': max_drawdown,
                        'profit_loss_ratio': abs(avg_win / avg_loss) if avg_loss > 0 else 0,
                        
                        # Risk metrics
                        'risk_reward_ratio': params['tp_distance'] / (params['tp_distance'] / 2),
                        'sharpe_ratio': strategy.total_pnl / max_drawdown if max_drawdown > 0 else 0,
                    })
            
            mt5.shutdown()
            
            # Export to CSV
            export_to_csv(results, f"optimization_results [{i}].csv")
            
            # Display summary
            display_summary(results)
        cumulative_results.append(results)
            
    return cumulative_results

def export_to_csv(results, filename=f'optimization_results.csv'):
    """Export optimization results to CSV file"""
    
    if not results:
        print("No results to export")
        return
    
    # Define field order
    fieldnames = [
        # Parameters
        'entry_distance', 'tp_distance', 'sl_distance', 'lot_size', 'steps',
        
        # Performance metrics
        'total_pnl', 'total_trades', 'winning_trades', 'losing_trades', 
        'win_rate', 'profit_factor',
        
        # Advanced metrics
        'avg_win', 'avg_loss', 'expectancy', 'max_drawdown', 'profit_loss_ratio',
        
        # Risk metrics
        'risk_reward_ratio', 'sharpe_ratio'
    ]
    
    # Create DataFrame
    df = pd.DataFrame(results)
    
    # Reorder columns
    df = df[fieldnames]
    
    # Sort by total_pnl (descending)
    df = df.sort_values('total_pnl', ascending=False)
    
    # Export to CSV
    df.to_csv(filename, index=False, float_format='%.4f')
    
    # Create a summary CSV with top performers
    create_summary_csv(df, filename)
    
    print(f"\n{'='*80}")
    print(f"CSV exported successfully: {filename}")
    print(f"Total parameter sets tested: {len(results)}")
    print(f"{'='*80}")

def create_summary_csv(df, base_filename):
    """Create summary CSV with various perspectives"""
    
    # Extract base name without extension
    base_name = os.path.splitext(base_filename)[0]
    
    # 1. Top 10 by total PnL
    top_10_pnl = df.nlargest(10, 'total_pnl')
    top_10_pnl.to_csv(f"{base_name}_top10_pnl.csv", index=False, float_format='%.4f')
    
    # 2. Top 10 by profit factor
    top_10_pf = df[df['total_trades'] > 5].nlargest(10, 'profit_factor')
    if not top_10_pf.empty:
        top_10_pf.to_csv(f"{base_name}_top10_profit_factor.csv", index=False, float_format='%.4f')
    
    # 3. Best risk-adjusted returns (Sharpe ratio)
    top_10_sharpe = df[df['total_trades'] > 5].nlargest(10, 'sharpe_ratio')
    if not top_10_sharpe.empty:
        top_10_sharpe.to_csv(f"{base_name}_top10_sharpe.csv", index=False, float_format='%.4f')
    
    # 4. Best win rate (with minimum trades)
    min_trades = 10
    high_volume = df[df['total_trades'] >= min_trades]
    if not high_volume.empty:
        top_10_winrate = high_volume.nlargest(10, 'win_rate')
        top_10_winrate.to_csv(f"{base_name}_top10_winrate.csv", index=False, float_format='%.4f')

def display_summary(results):
    """Display key summary statistics"""
    
    if not results:
        return
    
    df = pd.DataFrame(results)
    
    print(f"\n{'='*80}")
    print("OPTIMIZATION SUMMARY")
    print(f"{'='*80}")
    
    # Overall statistics
    print(f"\nOverall Statistics:")
    print(f"  Total parameter sets: {len(df)}")
    print(f"  Average P&L: ${df['total_pnl'].mean():.2f}")
    print(f"  Best P&L: ${df['total_pnl'].max():.2f}")
    print(f"  Worst P&L: ${df['total_pnl'].min():.2f}")
    print(f"  Early tps: {df['early_tps'].sum():.2f}")
    print(f"Phantom P&L: ${df['total_phantom_pnl'].sum():,.2f}")
    print(f"Mean P&L: ${df['avg_phantom_pnl'].sum():,.2f}")
    
    # Find top 3 performers
    top_3 = df.nlargest(3, 'total_pnl')
    
    print(f"\nTop 3 Parameter Sets by P&L:")
    for i, (_, row) in enumerate(top_3.iterrows(), 1):
        print(f"\n  #{i}:")
        print(f"    Entry: {row['entry_distance']}, TP: {row['tp_distance']}, Steps: {row['steps']}")
        print(f"    P&L: ${row['total_pnl']:.2f}, Win Rate: {row['win_rate']:.1f}%")
        print(f"    Profit Factor: {row['profit_factor']:.2f}, Trades: {row['total_trades']}")
    
    # Recommendations
    print(f"\n{'='*80}")
    print("RECOMMENDATIONS FOR DECISION MAKING:")
    print(f"{'='*80}")
    
    # Check for robust strategies (good metrics across multiple dimensions)
    robust_criteria = df[
        (df['total_trades'] >= 10) &  # Minimum trades
        (df['profit_factor'] > 1.5) &  # Good profit factor
        (df['win_rate'] > 40) &  # Reasonable win rate
        (df['expectancy'] > 0)  # Positive expectancy
    ]
    
    if not robust_criteria.empty:
        robust = robust_criteria.nlargest(3, 'sharpe_ratio')
        print(f"\nRobust Strategies (balanced performance):")
        for i, (_, row) in enumerate(robust.iterrows(), 1):
            print(f"  {i}. Entry={row['entry_distance']}, TP={row['tp_distance']}, "
                  f"PF={row['profit_factor']:.2f}, WR={row['win_rate']:.1f}%, "
                  f"Expectancy={row['expectancy']:.2f}")
    else:
        print("\nNo strategies met all robust criteria")
    
    # Check for high win rate strategies
    high_winrate = df[df['total_trades'] >= 5].nlargest(3, 'win_rate')
    if not high_winrate.empty:
        print(f"\nHigh Win Rate Strategies:")
        for i, (_, row) in enumerate(high_winrate.iterrows(), 1):
            print(f"  {i}. WR={row['win_rate']:.1f}%, P&L=${row['total_pnl']:.2f}, "
                  f"Trades={row['total_trades']}")
    
    print(f"\n{'='*80}")
    print("Check the generated CSV files for complete analysis:")
    print("  - optimization_results.csv: All results")
    print("  - optimization_results_top10_*.csv: Top performers by category")
    print(f"{'='*80}")

# Helper function to generate parameter combinations (if not already defined)
def generate_optimization_config(ranges):
    """Generate all combinations of parameters"""
    import itertools
    
    keys = ranges.keys()
    values = ranges.values()
    combinations = list(itertools.product(*values))
    
    return [dict(zip(keys, combo)) for combo in combinations]

if __name__ == "__main__":
    optimize_parameters()