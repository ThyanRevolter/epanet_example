""""
This module implements a water distribution network model using Pyomo without any loss or friction terms.
"""

import pyomo.environ as pyo
import numpy as np
import pandas as pd
from pyomo.opt import SolverFactory
from pyomo.environ import value
from datetime import datetime
from epanet_tutorial.simple_nr import WaterNetwork, Units
from electric_emission_cost import costs
import matplotlib.pyplot as plt
import os


class DynamicWaterNetwork():
    """
    A class to represent a simple dynamic water network.
    """
    def __init__(self, inp_file_path:str, ):
        self.n_time_steps = 24
        self.time_steps = range(self.n_time_steps)
        self.wn = WaterNetwork(inp_file_path, units=Units.IMPERIAL_CFS, round_to=3).wn
        self.start_dt = datetime(2025, 1, 1, 0, 0, 0)
        self.end_dt = datetime(2025, 1, 2, 0, 0, 0)
        self.model = pyo.ConcreteModel()
        self.create_model_variables()
        self.create_demand_constraints()
        self.create_tank_level_constraints()
        self.create_nodal_flow_balance_constraints()
        self.create_tank_flow_balance_constraints()
        self.create_pump_flow_constraints()
        self.create_pump_on_time_constraint()
        self.create_power_variables()
        self.create_total_power_constraint()
        self.results = None
        self.rate_df = pd.read_csv("data/tariffs/example_tariff.csv", sep=",")
        self.create_objective()

    def create_model_variables(self):
        """
        Create a Pyomo model variables for the water network.
        """
        
        self.model.t = range(self.n_time_steps)
        
        # pipe flow variables for each pipe for each time step
        for pipe in self.wn["links"]:
            if pipe["link_type"] == "Pipe":
                self.model.add_component(f"pipe_flow_{pipe['name']}", pyo.Var(self.time_steps, initialize=0))

        # pump flow variables for each pump for each time step
        for pump in self.wn["links"]:
            if pump["link_type"] == "Pump":
                self.model.add_component(f"pump_flow_{pump['name']}", pyo.Var(self.time_steps, initialize=0))

        # tank level variables for each tank for each time step
        for tank in self.wn["nodes"]:
            if tank["node_type"] == "Tank":
                self.model.add_component(f"tank_level_{tank['name']}", pyo.Var(self.time_steps, initialize=0))

        # demand variables for each demand node for each time step
        for demand_node in self.wn["nodes"]:
            if demand_node["node_type"] == "Junction" and demand_node["base_demand"] > 0:
                self.model.add_component(f"demand_{demand_node['name']}", pyo.Var(self.time_steps, initialize=0))

    def create_demand_pattern(self, base_demand:float, pattern_name:str):
        """
        Create a demand pattern for a demand node.
        """
        pattern_data = [pattern["multipliers"] for pattern in self.wn["patterns"] if pattern["name"] == pattern_name][0]
        pattern_values = np.array(pattern_data)
        return base_demand * pattern_values

    def create_demand_constraints(self):
        """
        Create constraints for the demand nodes.
        """
        for demand_node in self.wn["nodes"]:
            if demand_node["node_type"] == "Junction" and demand_node["base_demand"] > 0:
                demand_pattern = self.create_demand_pattern(demand_node["base_demand"], demand_node["demand_pattern"])
                self.model.add_component(f"demand_pattern_{demand_node['name']}", pyo.Param(self.time_steps, initialize=demand_pattern, mutable=True))
                for t in self.time_steps:
                    self.model.add_component(
                        f"demand_constraint_{demand_node['name']}_{t}", 
                        pyo.Constraint(expr=(
                            self.model.component(f"demand_{demand_node['name']}")[t] == self.model.component(f"demand_pattern_{demand_node['name']}")[t]
                        )))

    def create_tank_level_constraints(self):
        """
        Create constraints for the tank levels.
        """
        for tank in self.wn["nodes"]:
            if tank["node_type"] == "Tank":
                # initial level constraint
                self.model.add_component(
                    f"tank_level_init_{tank['name']}", 
                    pyo.Constraint(expr=(
                        self.model.component(f"tank_level_{tank['name']}")[0] == tank["init_level"]
                    )))
                for t in self.time_steps:
                    # minimum level constraint
                    self.model.add_component(
                        f"tank_level_min_{tank['name']}_{t}", 
                        pyo.Constraint(expr=(
                            self.model.component(f"tank_level_{tank['name']}")[t] >= tank["min_level"]
                        )))
                    # maximum level constraint
                    self.model.add_component(
                        f"tank_level_max_{tank['name']}_{t}", 
                        pyo.Constraint(expr=(
                            self.model.component(f"tank_level_{tank['name']}")[t] <= tank["max_level"]
                        )))

    def create_nodal_flow_balance_constraints(self):
        """
        Create constraints for the nodal flow balance.
        """
        for node in self.wn["nodes"]:
            if node["node_type"] == "Junction":
                for t in self.time_steps:
                    flow_pipe_in = {}
                    flow_pipe_out = {}
                    # pipes to the node
                    for pipe in self.wn["links"]:
                        if pipe["link_type"] == "Pipe" and pipe["start_node_name"] == node["name"]:
                            flow_pipe_out[pipe["name"]] = self.model.component(f"pipe_flow_{pipe['name']}")[t]
                        elif pipe["link_type"] == "Pipe" and pipe["end_node_name"] == node["name"]:
                            flow_pipe_in[pipe["name"]] = self.model.component(f"pipe_flow_{pipe['name']}")[t]
                    
                    flow_pump_in = {}
                    flow_pump_out = {}
                    # pumps to the node
                    for pump in self.wn["links"]:
                        if pump["link_type"] == "Pump" and pump["start_node_name"] == node["name"]:
                            flow_pump_out[pump["name"]] = self.model.component(f"pump_flow_{pump['name']}")[t]
                        elif pump["link_type"] == "Pump" and pump["end_node_name"] == node["name"]:
                            flow_pump_in[pump["name"]] = self.model.component(f"pump_flow_{pump['name']}")[t]
                    demand = self.model.component(f"demand_{node['name']}")[t] if node["base_demand"] > 0 else 0
                    flow_in = sum(flow_pipe_in.values()) + sum(flow_pump_in.values())
                    flow_out = sum(flow_pipe_out.values()) + sum(flow_pump_out.values())
                    self.model.add_component(
                        f"nodal_flow_balance_{node['name']}_{t}", 
                        pyo.Constraint(expr=(
                            flow_in == flow_out + demand
                        )))

    def create_tank_flow_balance_constraints(self):
        """
        Create constraints for the tank flow balance.
        """
        for tank in self.wn["nodes"]:
            if tank["node_type"] == "Tank":
                self.model.add_component(
                    f"tank_area_{tank['name']}", 
                    pyo.Param(initialize=tank["diameter"]**2 * np.pi / 4, default=tank["diameter"]**2 * np.pi / 4)
                )
                for t in self.time_steps[0:-1]:
                    flow_pipe_in = {}
                    flow_pipe_out = {}
                    # pipes to the tank
                    for pipe in self.wn["links"]:
                        if pipe["link_type"] == "Pipe" and pipe["start_node_name"] == tank["name"]:
                            flow_pipe_out[pipe["name"]] = self.model.component(f"pipe_flow_{pipe['name']}")[t]
                        elif pipe["link_type"] == "Pipe" and pipe["end_node_name"] == tank["name"]:
                            flow_pipe_in[pipe["name"]] = self.model.component(f"pipe_flow_{pipe['name']}")[t]
                    flow_in = sum(flow_pipe_in.values()) * 0.133681 * 60 # convert gallons per second to cubic feet per hour
                    flow_out = sum(flow_pipe_out.values()) * 0.133681 * 60 # convert gallons per second to cubic feet per hour
                    # tank dynamic level constraint
                    self.model.add_component(
                        f"tank_level_dynamic_{tank['name']}_{t+1}", 
                        pyo.Constraint(expr=(
                            self.model.component(f"tank_level_{tank['name']}")[t+1] == self.model.component(f"tank_level_{tank['name']}")[t] + ((flow_in - flow_out) / self.model.component(f"tank_area_{tank['name']}"))
                        )))

    def create_pump_flow_constraints(self):
        """
        Create constraints for the pump flow.
        """
        self.model.pump_flow_capacity = pyo.Param(initialize=150, mutable=True)
        for pump in self.wn["links"]:
            if pump["link_type"] == "Pump":
                # add binary variable for pump on/off
                self.model.add_component(
                    f"pump_on_status_{pump['name']}", 
                    pyo.Var(self.time_steps, initialize=0, domain=pyo.Binary)
                )
                for t in self.time_steps:
                    # pump flow constraint
                    self.model.add_component(
                        f"pump_flow_{pump['name']}_{t}", 
                        pyo.Constraint(expr=(
                            self.model.component(f"pump_flow_{pump['name']}")[t] == self.model.component(f"pump_on_status_{pump['name']}")[t] * self.model.pump_flow_capacity
                        )))

    def create_pump_on_time_constraint(self):
        """
        Create constraints for the pump on/off status.
        """
        self.model.pump_max_on_time = pyo.Param(initialize=10, mutable=True)
        for pump in self.wn["links"]:
            if pump["link_type"] == "Pump":
                self.model.add_component(
                    f"max_pump_on_time_constraint_{pump['name']}",
                    pyo.Constraint(expr=(
                        sum([self.model.component(f"pump_on_status_{pump['name']}")[t] for t in self.time_steps]) <= self.model.pump_max_on_time
                    )))

    def create_power_variables(self):
        """
        Create variables for the power.
        """
        self.model.pump_power_capacity = pyo.Param(initialize=10, default=10)
        for pump in self.wn["links"]:
            if pump["link_type"] == "Pump":
                self.model.add_component(
                    f"pump_power_{pump['name']}", 
                    pyo.Var(self.time_steps, initialize=0, domain=pyo.NonNegativeReals)
                )
                for t in self.time_steps:
                    # pump power constraint
                    self.model.add_component(
                        f"pump_power_constraint_{pump['name']}_{t}", 
                        pyo.Constraint(expr=(
                            self.model.component(f"pump_power_{pump['name']}")[t] == self.model.component(f"pump_on_status_{pump['name']}")[t] * self.model.pump_power_capacity
                        )))

    def create_total_power_constraint(self):
        """
        Create a variable for the total power at each time step and a constraint for it.
        """
        self.model.add_component(
            "total_power", 
            pyo.Var(self.time_steps, initialize=0, domain=pyo.NonNegativeReals)
        )
        # add all pump power variables to the total power constraint
        for t in self.time_steps:
            total_power_expression = {}
            for pump in self.wn["links"]:
                if pump["link_type"] == "Pump":
                    total_power_expression[pump["name"]] = self.model.component(f"pump_power_{pump['name']}")[t]
            self.model.add_component(
                f"total_power_constraint_{t}", 
                pyo.Constraint(expr=(
                    self.model.component("total_power")[t] == sum(total_power_expression.values())
                ))
            )

    def create_objective(self):
        """
        Create an objective function for the model.
        """
        self.charge_dict = costs.get_charge_dict(self.start_dt, self.end_dt, self.rate_df, resolution="1h")
        consumption_data_dict = {"electric": self.model.component("total_power")}
        self.model.electricity_cost, self.model = costs.calculate_cost(
            self.charge_dict,
            consumption_data_dict,
            resolution="1h",
            prev_demand_dict=None,
            prev_consumption_dict=None,
            consumption_estimate=0,
            desired_utility="electric",
            desired_charge_type=None,
            model=self.model,
        )
        
        self.model.add_component(
            "objective", 
            pyo.Objective(expr=self.model.electricity_cost, sense=pyo.minimize)
        )

    def solve(self):
        """
        Solve the model.
        """
        solver = SolverFactory("gurobi")
        solver.options["MIPGap"] = 0.01
        solver.options["TimeLimit"] = 100
        solver.options["OptimalityTol"] = 1e-6
        results = solver.solve(
            self.model,
            tee=True,
        )
        self.results = results

    def package_flows_results(self):
        """
        Package the results into a pandas dataframe.
        """
        flows_df = pd.DataFrame()
        flows_df["time"] = pd.date_range(start=self.start_dt, periods=self.n_time_steps, freq="h")
        for pipe in self.wn["links"]:
            if pipe["link_type"] == "Pipe":
                flows_df[pipe["name"]] = [value(self.model.component(f"pipe_flow_{pipe['name']}")[t]) for t in self.time_steps]
        for pump in self.wn["links"]:
            if pump["link_type"] == "Pump":
                flows_df[pump["name"]] = [value(self.model.component(f"pump_flow_{pump['name']}")[t]) for t in self.time_steps]
        return flows_df

    def package_tank_results(self):
        """
        Package the tank results into a pandas dataframe.
        """
        tank_df = pd.DataFrame()
        tank_df["time"] = pd.date_range(start=self.start_dt, periods=self.n_time_steps, freq="h")
        for tank in self.wn["nodes"]:
            if tank["node_type"] == "Tank":
                tank_df[tank["name"]] = [value(self.model.component(f"tank_level_{tank['name']}")[t]) for t in self.time_steps]
        return tank_df

    def package_demand_results(self):
        """
        Package the demand results into a pandas dataframe.
        """
        demand_df = pd.DataFrame()
        demand_df["time"] = pd.date_range(start=self.start_dt, periods=self.n_time_steps, freq="h")
        for demand_node in self.wn["nodes"]:
            if demand_node["node_type"] == "Junction" and demand_node["base_demand"] > 0:
                demand_df[demand_node["name"]] = [value(self.model.component(f"demand_{demand_node['name']}")[t]) for t in self.time_steps]
        return demand_df

    def package_power_results(self):
        """
        Package the power results into a pandas dataframe.
        """
        power_df = pd.DataFrame()
        power_df["time"] = pd.date_range(start=self.start_dt, periods=self.n_time_steps, freq="h")
        for pump in self.wn["links"]:
            if pump["link_type"] == "Pump":
                power_df[pump["name"]] = [value(self.model.component(f"pump_power_{pump['name']}")[t]) for t in self.time_steps]
        power_df["total_power"] = [value(self.model.component("total_power")[t]) for t in self.time_steps]
        return power_df

    def check_nodal_flow_balance(self):
        """
        check if the nodal flow balance are satisfied
        """
        for node in self.wn["nodes"]:
            if node["node_type"] == "Junction":
                for t in self.time_steps:
                    flow_in = 0
                    flow_out = 0
                    pump_in = 0
                    pump_out = 0
                    for pipe in self.wn["links"]:
                        if pipe["link_type"] == "Pipe" and pipe["start_node_name"] == node["name"]:
                            flow_out += value(self.model.component(f"pipe_flow_{pipe['name']}")[t])
                        elif pipe["link_type"] == "Pipe" and pipe["end_node_name"] == node["name"]:
                            flow_in += value(self.model.component(f"pipe_flow_{pipe['name']}")[t])
                        elif pipe["link_type"] == "Pump" and pipe["start_node_name"] == node["name"]:
                            pump_out += value(self.model.component(f"pump_flow_{pipe['name']}")[t])
                        elif pipe["link_type"] == "Pump" and pipe["end_node_name"] == node["name"]:
                            pump_in += value(self.model.component(f"pump_flow_{pipe['name']}")[t])
                    demand = value(self.model.component(f"demand_{node['name']}")[t]) if node["base_demand"] > 0 else 0
                    if abs(flow_in - flow_out - demand + pump_in - pump_out) > 1e-6:
                        print(f"Nodal flow balance not satisfied for node {node['name']} at time {t}")
                        print(f"flow_in: {flow_in}, flow_out: {flow_out}, demand: {demand}, pump_in: {pump_in}, pump_out: {pump_out}")
                        return False
        return True

    def plot_results(self):
        """
        plot pipe flows, pump flows, tank levels, demand, and power.
        """
        fig, axs = plt.subplots(6, 1, figsize=(10, 30))
        for pipe in self.wn["links"]:
            if pipe["link_type"] == "Pipe":
                axs[0].plot(self.package_flows_results()[pipe["name"]], label=pipe["name"])
        axs[0].legend()
        axs[0].set_title("Pipe Flows")
        axs[0].set_ylabel("Flow (gpm)")
        for pump in self.wn["links"]:
            if pump["link_type"] == "Pump":
                axs[1].plot(self.package_flows_results()[pump["name"]], label=pump["name"])
        axs[1].legend()
        axs[1].set_title("Pump Flows")
        axs[1].set_ylabel("Flow (gpm)")
        for tank in self.wn["nodes"]:
            if tank["node_type"] == "Tank":
                axs[2].plot(self.package_tank_results()[tank["name"]], label=tank["name"])
        axs[2].legend()
        axs[2].set_title("Tank Levels")
        axs[2].set_ylabel("Level (ft)")
        for demand_node in self.wn["nodes"]:
            if demand_node["node_type"] == "Junction" and demand_node["base_demand"] > 0:
                axs[3].plot(self.package_demand_results()[demand_node["name"]], label=demand_node["name"])
        axs[3].legend()
        axs[3].set_title("Demand")
        axs[3].set_ylabel("Demand (gpm)")
        axs[4].plot(self.package_power_results()["total_power"], label="total power")
        axs[4].legend()
        axs[4].set_title("Power")
        axs[4].set_ylabel("Power (kW)")
        # plot the electricity cost
        electricity_charges = sum(self.charge_dict.values())
        axs[5].plot(electricity_charges, label="electricity charges")
        axs[5].legend()
        axs[5].set_title("Electricity Charges")
        axs[5].set_ylabel("Electricity Charges ($/kWh)")
        # make sure the directory exists
        os.makedirs("data/local/plots", exist_ok=True)
        plt.savefig("data/local/plots/results.png")
        return fig, axs

    def print_model_info(self, save_to_file:bool=False):
        """
        Print model information including variables and constraints in a structured way and save it to a text file.
        Args:
            save_to_file (bool): If True, saves the output to a text file named 'model_info.txt'
        """
        # Create output string
        output = []
        output.append("\n" + "="*50)
        output.append("MODEL INFORMATION")
        output.append("="*50)
        
        # Print total number of variables and constraints
        output.append("\nMODEL STATISTICS:")
        output.append("-"*30)
        n_vars = len(self.model.component_map(pyo.Var))
        n_cons = len(self.model.component_map(pyo.Constraint))
        output.append(f"Total Variables: {n_vars}")
        output.append(f"Total Constraints: {n_cons}")
        
        # Print Variables
        output.append("\nVARIABLES:")
        output.append("-"*30)
        for var_name, var in self.model.component_map(pyo.Var).items():
            output.append(f"\n{var_name}:")
            output.append(f"  Type: {type(var).__name__}")
            if hasattr(var, 'index_set'):
                output.append(f"  Index Set: {var.index_set()}")
                # For indexed variables, get domain from first index
                if hasattr(var, '__getitem__'):
                    first_idx = next(iter(var.index_set()))
                    output.append(f"  Domain: {var[first_idx].domain}")
                else:
                    output.append(f"  Domain: {var.domain}")
        
        # print Parameters
        output.append("\nPARAMETERS:")
        output.append("-"*30)
        for param_name, param in self.model.component_map(pyo.Param).items():
            output.append(f"\n{param_name}:")
            output.append(f"  Type: {type(param).__name__}")
            if hasattr(param, 'index_set'):
                output.append(f"  Index Set: {param.index_set()}")
                # For indexed parameters, print value for first index
                if hasattr(param, '__getitem__'):
                    first_idx = next(iter(param.index_set()))
                    output.append(f"  Value (first index): {param[first_idx]}")
                else:
                    output.append(f"  Value: {param.value}")
            else:
                output.append(f"  Value: {param.value}")
        
        # Print Constraints
        output.append("\nCONSTRAINTS:")
        output.append("-"*30)
        for con_name, con in self.model.component_map(pyo.Constraint).items():
            output.append(f"\n{con_name}:")
            output.append(f"  Type: {type(con).__name__}")
            if hasattr(con, 'index_set'):
                output.append(f"  Index Set: {con.index_set()}")
                # For indexed constraints, print expression for first index
                if hasattr(con, '__getitem__'):
                    first_idx = next(iter(con.index_set()))
                    output.append("  Expression (first index):")
                    output.append(f"    {con[first_idx].expr}")
                else:
                    output.append("  Expression:")
                    output.append(f"    {con.expr}")
            else:
                output.append("  Expression:")
                output.append(f"    {con.expr}")
        
        # print the objective function
        output.append("\nOBJECTIVE FUNCTION:")
        output.append("-"*30)
        output.append(f"  Expression: {self.model.objective.expr}")

        output.append("\n" + "="*50)        
        
        # Save to file if requested
        if save_to_file:
            with open(f'data/local/model_info_{datetime.now().strftime("%Y%m%d_%H%M%S")}.txt', 'w') as f:
                f.write("\n".join(output))
        else:
            print("\n".join(output))

if __name__ == "__main__":
    wdn = DynamicWaterNetwork("data/epanet_networks/simple_pump_tank.inp")
    wdn.print_model_info(save_to_file=True)
    wdn.solve()
    flows_df = wdn.package_flows_results()
    tank_df = wdn.package_tank_results()
    demand_df = wdn.package_demand_results()
    power_df = wdn.package_power_results()
    # make sure the directory exists
    os.makedirs("data/local/operational_data", exist_ok=True)
    flows_df.to_csv("data/local/operational_data/flows_df.csv", index=False)
    tank_df.to_csv("data/local/operational_data/tank_df.csv", index=False)
    demand_df.to_csv("data/local/operational_data/demand_df.csv", index=False)
    power_df.to_csv("data/local/operational_data/power_df.csv", index=False)

    # itemized cost
    power_consumed = power_df["total_power"].values
    itemized_cost = costs.calculate_itemized_cost(
        wdn.charge_dict,
        {"electric": power_consumed},
        resolution="1h"
    )
    print(itemized_cost)
    wdn.plot_results()