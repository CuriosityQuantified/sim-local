"""
Simulation Service Module

This module provides the main orchestration service for the simulation system.
It coordinates the entire simulation flow including scenario generation, 
selection, media generation, and state management.
"""

import logging
from typing import Dict, Any, List, Optional
from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate
import json
import traceback

from services.llm_service import LLMService
from services.state_service import StateService
from services.media_service import MediaService
from models.simulation import SimulationState, Scenario, LLMLog

logger = logging.getLogger(__name__)

class SimulationService:
    """
    Service for orchestrating the simulation flow.
    
    Coordinates interactions between various services (LLM, State, Media)
    to execute the complete simulation flow.
    """
    
    def __init__(
        self, 
        llm_service: LLMService, 
        state_service: StateService,
        media_service: MediaService
    ):
        """
        Initialize the simulation service.
        
        Args:
            llm_service: Service for LLM interactions
            state_service: Service for state management
            media_service: Service for media generation
        """
        self.llm_service = llm_service
        self.state_service = state_service
        self.media_service = media_service
        
        # Set up the logging callback for the LLM service
        self.llm_service.set_log_callback(self._log_llm_interaction)
    
    async def _log_llm_interaction(self, turn_number: int, llm_log: LLMLog) -> None:
        """
        Callback for logging LLM interactions.
        
        Args:
            turn_number: The turn number the log belongs to
            llm_log: The LLM log to store
        """
        # Find the simulation that's currently being processed
        # This is a simplification - in a real system, we'd need to track which simulation
        # is associated with each LLM call
        simulations = self.state_service.get_all_simulations()
        if not simulations:
            logger.warning("No simulations found for LLM log")
            return
            
        # For now, we'll log to the most recently created simulation
        simulations.sort(key=lambda s: s.created_at, reverse=True)
        simulation = simulations[0]
        
        # Only log if developer mode is enabled
        if simulation.developer_mode:
            logger.info(f"Logging LLM interaction for simulation {simulation.simulation_id}, turn {turn_number}")
            simulation.add_llm_log(turn_number, llm_log)
            self.state_service.update_simulation(simulation)
        
    async def create_new_simulation(self, initial_prompt: Optional[str] = None, developer_mode: bool = False) -> SimulationState:
        """
        Create a new simulation.
        
        Args:
            initial_prompt: Optional user prompt to guide the initial scenario generation
            developer_mode: Whether to enable developer mode with detailed LLM logging
            
        Returns:
            The new SimulationState
        """
        try:
            # Create a new simulation state
            simulation = SimulationState()
            simulation.developer_mode = developer_mode
            
            # Add it to the state service
            self.state_service.create_simulation(simulation)
            
            # Generate the initial scenarios
            context = {
                "simulation_history": "",
                "current_turn_number": 1,
                "previous_turn_number": 0,
                "user_prompt_for_this_turn": initial_prompt or "",
                "max_turns": simulation.max_turns
            }
            
            # Generate a single scenario
            scenario = await self.llm_service.create_idea(context)
            logger.info(f"Successfully generated a scenario")
            
            # Log the generated scenario
            logger.info(f"Generated scenario for turn 1")
            scenario_id = scenario.get("id", "unknown_1")
            logger.info(f"Scenario ID: {scenario_id}")
            logger.debug(f"Scenario Description: {scenario.get('situation_description', '')[:50]}...")
            
            # Convert scenario to the Scenario model object with validation
            try:
                # Ensure all required fields exist with defaults if missing
                scenario_id = scenario.get("id", "scenario_1_1")
                description = scenario.get("situation_description", "Default scenario")
                rationale = scenario.get("rationale", "Auto-generated")
                user_role = scenario.get("user_role", "Crisis Response Specialist tasked with solving this absurd global threat")
                user_prompt = scenario.get("user_prompt", "What strategy will you implement to address this situation and save the world?")
                
                scenario_model = Scenario(
                    id=scenario_id,
                    situation_description=description,
                    rationale=rationale,
                    user_role=user_role,
                    user_prompt=user_prompt
                )
                
                # Add the scenario to the simulation
                simulation.add_scenarios(1, [scenario_model])
                
                # Automatically select the scenario
                simulation.select_scenario(1, scenario_id)
                
                # Generate media prompts - video prompt only
                video_prompt = await self.llm_service.create_video_prompt(scenario, turn_number=1)
                
                # Add media prompts to the simulation state - set narration_script to None
                simulation.add_media_prompts(1, video_prompt, None)
                
                # Generate media (video and audio in parallel)
                media_results = await self.media_service.generate_media_parallel(scenario, video_prompt, turn=1)
                
                # Add media URLs to the simulation state
                simulation.add_media_urls(1, media_results['video_urls'], media_results['audio_url'])
            except Exception as e:
                logger.error(f"Error processing scenario: {str(e)}")
                raise
            
            # Update the simulation in the state service
            self.state_service.update_simulation(simulation)
            
            return simulation
        except Exception as e:
            logger.error(f"Unexpected error in create_new_simulation: {str(e)}")
            logger.error(traceback.format_exc())
            raise
    
    async def process_user_response(self, simulation_id: str, user_response: str) -> Optional[SimulationState]:
        """
        Process a user response and generate the next turn in the simulation.
        
        Args:
            simulation_id: The ID of the simulation
            user_response: The user's response text
            
        Returns:
            The updated SimulationState, or None if the simulation wasn't found
        """
        try:
            # Get the simulation
            simulation = self.state_service.get_simulation(simulation_id)
            if not simulation:
                logger.error(f"Simulation not found: {simulation_id}")
                return None
            
            # Get the current turn number
            current_turn = simulation.current_turn_number
            
            # Add the user response to the current turn
            simulation.add_user_response(current_turn, user_response)
            
            # If the simulation was already marked complete (e.g., navigating back to a completed sim), just save and return
            if simulation.is_complete:
                self.state_service.update_simulation(simulation)
                return simulation
            
            # Determine if the turn just played was the last playable turn
            is_last_playable_turn = (current_turn == simulation.max_turns)

            if is_last_playable_turn:
                # Generate the CONCLUSION turn
                conclusion_turn_number = current_turn + 1
                simulation.current_turn_number = conclusion_turn_number # Advance to the conclusion turn number
                
                logger.info(f"Processing response for turn {current_turn}/{simulation.max_turns}. Generating CONCLUSION for turn {conclusion_turn_number}.")
                
                context = {
                    "simulation_history": simulation.get_history_text(),
                    "current_turn_number": conclusion_turn_number, # This will make LLMService generate a conclusion
                    "previous_turn_number": current_turn,
                    "user_prompt_for_this_turn": "", # No specific user prompt for conclusion generation itself
                    "max_turns": simulation.max_turns
                }
                # Media generation will occur based on the scenario from create_idea
                # is_complete will be set to True after this block
            else:
                # Generate the NEXT REGULAR turn's scenarios
                next_playable_turn = current_turn + 1
                simulation.current_turn_number = next_playable_turn # Advance to the next playable turn
                
                logger.info(f"Processing response for turn {current_turn}/{simulation.max_turns}. Generating next scenario for turn {next_playable_turn}.")

                context = {
                    "simulation_history": simulation.get_history_text(),
                    "current_turn_number": next_playable_turn,
                    "previous_turn_number": current_turn,
                    "user_prompt_for_this_turn": "", # Default for regular next turn
                    "max_turns": simulation.max_turns
                }
                # is_complete remains False
            
            # Add the updated simulation to state service before generating the next scenario
            # This ensures the user response and current_turn_number update are saved
            self.state_service.update_simulation(simulation)
            
            try:
                # Generate a single scenario (either next playable or conclusion based on context.current_turn_number)
                scenario = await self.llm_service.create_idea(context)
                logger.info(f"Successfully generated a scenario for turn {simulation.current_turn_number}")
                
                # Log the generated scenario
                logger.info(f"Generated scenario for turn {simulation.current_turn_number}")
                scenario_id = scenario.get("id", f"scenario_{simulation.current_turn_number}_1")
                logger.info(f"Scenario ID: {scenario_id}")
                logger.debug(f"Scenario Description: {scenario.get('situation_description', '')[:50]}...")
                
                # Ensure all required fields exist with defaults if missing
                description = scenario.get("situation_description", f"Default scenario for turn {simulation.current_turn_number}")
                rationale = scenario.get("rationale", "Auto-generated")
                
                # Grade, user_role, user_prompt are handled by _validate_scenario in LLMService based on turn context
                # For conclusion turn, LLMService will include grade/grade_explanation in 'scenario' object
                # For playable turns, it will include user_role/user_prompt.
                
                scenario_model_fields = {
                    "id": scenario_id,
                    "situation_description": description,
                    "rationale": rationale
                }
                if "user_role" in scenario: scenario_model_fields["user_role"] = scenario["user_role"]
                if "user_prompt" in scenario: scenario_model_fields["user_prompt"] = scenario["user_prompt"]
                if "grade" in scenario: scenario_model_fields["grade"] = scenario["grade"]
                if "grade_explanation" in scenario: scenario_model_fields["grade_explanation"] = scenario["grade_explanation"]

                scenario_model = Scenario(**scenario_model_fields)
                
                # Add the scenario to the simulation
                simulation.add_scenarios(simulation.current_turn_number, [scenario_model])
                
                # Automatically select the scenario
                simulation.select_scenario(simulation.current_turn_number, scenario_id)
                
                # Generate media prompts - video prompt only
                video_prompt = await self.llm_service.create_video_prompt(scenario, turn_number=simulation.current_turn_number)
                
                # Add media prompts to the simulation state - set narration_script to None
                simulation.add_media_prompts(simulation.current_turn_number, video_prompt, None)
                
                # Generate media (video and audio in parallel) - THIS WILL RUN FOR CONCLUSION TURN TOO
                media_results = await self.media_service.generate_media_parallel(scenario, video_prompt, turn=simulation.current_turn_number)
                
                # Add media URLs to the simulation state
                simulation.add_media_urls(simulation.current_turn_number, media_results['video_urls'], media_results['audio_url'])
                
                # Set simulation as complete if this was the conclusion turn generation
                if is_last_playable_turn:
                    simulation.is_complete = True
                    logger.info(f"Simulation {simulation_id} marked as complete after generating conclusion for turn {simulation.current_turn_number}.")

            except Exception as scenario_error:
                logger.error(f"Error processing scenario for turn {simulation.current_turn_number}: {str(scenario_error)}")
                
                # Create a fallback scenario
                fallback_scenario_fields = {
                    "id": f"scenario_{simulation.current_turn_number}_1",
                    "situation_description": f"Communication issues have affected our analysis systems. Please provide your assessment based on previous information.",
                    "rationale": "System-generated fallback due to processing error",
                }
                # If it's not a conclusion, add role/prompt
                if not is_last_playable_turn:
                    fallback_scenario_fields["user_role"] = "Crisis Response Specialist"
                    fallback_scenario_fields["user_prompt"] = "How would you address the ongoing situation given the information available to you?"
                else: # Fallback for a conclusion turn might include a default grade
                    fallback_scenario_fields["grade"] = 50
                    fallback_scenario_fields["grade_explanation"] = "Conclusion generation failed due to system error. Default grade assigned."

                fallback_scenario = Scenario(**fallback_scenario_fields)
                
                # Add the fallback scenario
                simulation.add_scenarios(simulation.current_turn_number, [fallback_scenario])
                simulation.select_scenario(simulation.current_turn_number, fallback_scenario.id)
                
                # Add fallback media (empty for now, or define static fallbacks)
                simulation.add_media_prompts(simulation.current_turn_number, [], None)
                simulation.add_media_urls(simulation.current_turn_number, [], None)
                logger.info(f"Added fallback scenario and empty media for turn {simulation.current_turn_number}.")

                # If conclusion generation failed, still mark simulation as complete
                if is_last_playable_turn:
                    simulation.is_complete = True
                    logger.info(f"Simulation {simulation_id} marked as complete even after fallback for conclusion turn {simulation.current_turn_number}.")
            
            # Update the simulation in the state service
            self.state_service.update_simulation(simulation)
            
            return simulation
        except Exception as e:
            logger.error(f"Unexpected error in process_user_response: {str(e)}")
            logger.error(traceback.format_exc())
            try:
                # Try to recover the simulation state if possible
                if 'simulation' in locals() and simulation:
                    self.state_service.update_simulation(simulation)
                    return simulation
            except:
                pass
            raise
    
    async def toggle_developer_mode(self, simulation_id: str, enabled: bool) -> Optional[SimulationState]:
        """
        Toggle developer mode for a simulation.
        
        Args:
            simulation_id: The ID of the simulation
            enabled: Whether to enable or disable developer mode
            
        Returns:
            The updated SimulationState, or None if the simulation wasn't found
        """
        try:
            # Get the simulation
            simulation = self.state_service.get_simulation(simulation_id)
            if not simulation:
                logger.error(f"Simulation not found: {simulation_id}")
                return None
            
            # Update the developer mode flag
            simulation.developer_mode = enabled
            
            # Update the simulation in the state service
            self.state_service.update_simulation(simulation)
            
            return simulation
        except Exception as e:
            logger.error(f"Error toggling developer mode: {str(e)}")
            return None 