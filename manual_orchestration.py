import os
import time
from typing import Dict, Optional

from azure.ai.projects import AIProjectClient
from azure.ai.agents.models import ToolSet, FunctionTool, MessageRole
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from agent_tools import (
    get_jira_ticket_description, get_pull_request_body,
    get_control_plan_metrics_from_pr_comment, get_jira_ticket_title, 
    get_jira_ticket_release_notes, get_jira_ticket_attachments,
    get_jira_ticket_xlsx_attachment,
    get_pav_metrics_for_apr, 
    get_ppa_metrics_for_apr, 
    get_sup_metrics_for_apr, 
    get_dup_metrics_for_apr,
    get_PRs_from_apr, get_pull_request_title, get_feature_rankings
)
from agent import Agent
from metric_instructions import build_metric_agent_instructions
from coordinator_instructions import get_coordinator_instructions

load_dotenv()

project_endpoint = os.environ["AZURE_EXISTING_AIPROJECT_ENDPOINT"]
model_name = os.environ["MODEL_DEPLOYMENT_NAME"]

project_client = AIProjectClient(
    endpoint=project_endpoint,
    credential=DefaultAzureCredential(),
)

class ManualOrchestration:
    def __init__(self, agents_client, model_deployment_name):
        self.agents_client = agents_client
        self.model_deployment_name = model_deployment_name
        self.agents = {}
        self.threads = {}
        
        # General tools for the coordinator (includes JIRA correlation tools)
        self.general_function_set = {
            get_jira_ticket_description, get_pull_request_body, get_pull_request_title,
            get_control_plan_metrics_from_pr_comment, get_jira_ticket_title, 
            get_jira_ticket_release_notes, get_jira_ticket_xlsx_attachment, 
            get_jira_ticket_attachments, get_PRs_from_apr, get_feature_rankings
        }
        
    def create_agents(self):
        """Create all 5 agents: PAV, PPA, SUP, DUP, and Coordinator"""
        print("🔧 Creating specialized metric agents...")
        
        # Create PAV agent
        pav_agent = Agent(
            name="PAV_Agent",
            instructions=build_metric_agent_instructions('PAV'),
            model=self.model_deployment_name,
            functions={get_pav_metrics_for_apr, get_pull_request_title, 
                      get_jira_ticket_title, get_jira_ticket_description, get_feature_rankings},
            metadata={"timeout": 360}  # 6 minute timeout
        )
        self.agents['pav'] = self.agents_client.create_agent(**pav_agent.to_create_params())
        
        # Create PPA agent
        ppa_agent = Agent(
            name="PPA_Agent",
            instructions=build_metric_agent_instructions('PPA'),
            model=self.model_deployment_name,
            functions={get_ppa_metrics_for_apr, get_pull_request_title,
                      get_jira_ticket_title, get_jira_ticket_description, get_feature_rankings},
            metadata={"timeout": 360}  # 6 minute timeout
        )
        self.agents['ppa'] = self.agents_client.create_agent(**ppa_agent.to_create_params())
        
        # Create SUP agent
        sup_agent = Agent(
            name="SUP_Agent",
            instructions=build_metric_agent_instructions('SUP'),
            model=self.model_deployment_name,
            functions={get_sup_metrics_for_apr, get_pull_request_title,
                      get_jira_ticket_title, get_jira_ticket_description, get_feature_rankings},
            metadata={"timeout": 360}  # 6 minute timeout
        )
        self.agents['sup'] = self.agents_client.create_agent(**sup_agent.to_create_params())
        
        # Create DUP agent
        dup_agent = Agent(
            name="DUP_Agent",
            instructions=build_metric_agent_instructions('DUP'),
            model=self.model_deployment_name,
            functions={get_dup_metrics_for_apr, get_pull_request_title,
                      get_jira_ticket_title, get_jira_ticket_description, get_feature_rankings},
            metadata={"timeout": 360}  # 6 minute timeout
        )
        self.agents['dup'] = self.agents_client.create_agent(**dup_agent.to_create_params())
        
        # Create Coordinator agent
        coordinator_config = Agent(
            name="APR_Coordinator",
            instructions=get_coordinator_instructions(),
            model=self.model_deployment_name,
            functions=self.general_function_set
        )
        self.agents['coordinator'] = self.agents_client.create_agent(**coordinator_config.to_create_params())
        
        # Create threads for each agent
        for agent_type in self.agents.keys():
            self.threads[agent_type] = self.agents_client.threads.create()
            
        print(f"✅ Created {len(self.agents)} agents successfully")
        
    def run_metric_analysis(self, apr_number: str, agent_type: str, retries: int = 2) -> str:
        """Run analysis for a specific metric agent with retry logic"""
        agent = self.agents[agent_type]
        thread = self.threads[agent_type]
        
        for attempt in range(retries + 1):
            try:
                if attempt > 0:
                    print(f"🔄 Retry {attempt}: Running {agent_type.upper()} analysis for APR {apr_number}...")
                else:
                    print(f"🔄 Running {agent_type.upper()} analysis for APR {apr_number}...")
                
                # Send message to the specific agent
                message = self.agents_client.messages.create(
                    thread_id=thread.id,
                    role="user",
                    content=f"Please analyze APR {apr_number} using your specialized metric analysis function. Focus on the most significant patterns to avoid timeout issues."
                )
                
                # Use the timeout configured in agent metadata (360 seconds = 6 minutes)
                timeout = 360  # All agents configured with 6-minute timeout
                run = self.agents_client.runs.create_and_process(
                    thread_id=thread.id,
                    agent_id=agent.id,
                    timeout=timeout
                )
                
                # Get the response
                messages = list(self.agents_client.messages.list(thread_id=thread.id))
                
                if messages and messages[0].role == MessageRole.AGENT:
                    result = messages[0].content[0].text.value
                    print(f"✅ {agent_type.upper()} analysis completed")
                    return result
                else:
                    if attempt < retries:
                        print(f"⚠️ No response from {agent_type.upper()} agent, retrying...")
                        time.sleep(5)  # Brief pause before retry
                        continue
                    else:
                        error_msg = f"❌ {agent_type.upper()} agent failed to provide analysis after {retries + 1} attempts"
                        print(error_msg)
                        return error_msg
                        
            except Exception as e:
                if attempt < retries:
                    print(f"⚠️ {agent_type.upper()} agent error: {e}, retrying...")
                    time.sleep(5)  # Brief pause before retry
                    continue
                else:
                    error_msg = f"❌ {agent_type.upper()} agent execution failed after {retries + 1} attempts: {e}"
                    print(error_msg)
                    return error_msg
            
    def create_final_report(self, apr_number: str, pav_result: str, ppa_result: str, 
                           sup_result: str, dup_result: str) -> str:
        """Use coordinator agent to create final comprehensive report"""
        print("🔄 Creating comprehensive final report...")
        
        coordinator = self.agents['coordinator']
        thread = self.threads['coordinator']
        
        # Provide data to coordinator without redundant instructions
        # The coordinator already knows its workflow from coordinator_instructions.py
        prompt = f"""Please analyze and synthesize the results for APR {apr_number}.

        Here are the metric analysis findings from the specialized agents:

        PAV AGENT ANALYSIS:
        {pav_result}

        PPA AGENT ANALYSIS:
        {ppa_result}

        SUP AGENT ANALYSIS:
        {sup_result}

        DUP AGENT ANALYSIS:
        {dup_result}

        Please create your comprehensive analysis following your instructions."""
        
        # Send to coordinator
        self.agents_client.messages.create(
            thread_id=thread.id,
            role="user",
            content=prompt
        )
        
        # Process final report with extended timeout for JIRA analysis
        print("⏳ Coordinator analyzing patterns and linking JIRA tickets...")
        run = self.agents_client.runs.create_and_process(
            thread_id=thread.id,
            agent_id=coordinator.id,
            timeout=600  # 10 minutes for comprehensive JIRA analysis
        )
        
        # Get final report
        messages = list(self.agents_client.messages.list(thread_id=thread.id))
        if messages and messages[0].role == MessageRole.AGENT:
            final_report = messages[0].content[0].text.value
            print("✅ Comprehensive report completed")
            return final_report
        else:
            return "❌ Failed to generate comprehensive report"
            
    def analyze_apr(self, apr_number: str) -> str:
        # Run all metric analyses in parallel concept (sequential for now, could be parallelized)
        results = {}
        
        # Collect all individual analyses
        results['pav'] = self.run_metric_analysis(apr_number, 'pav')
        results['ppa'] = self.run_metric_analysis(apr_number, 'ppa') 
        results['sup'] = self.run_metric_analysis(apr_number, 'sup')
        results['dup'] = self.run_metric_analysis(apr_number, 'dup')
        
        # Create final comprehensive report
        final_report = self.create_final_report(
            apr_number, 
            results['pav'], 
            results['ppa'],
            results['sup'], 
            results['dup']
        )
        print(f"🎉 APR {apr_number} analysis complete!")
        return final_report
        
    def chat_mode(self):
        """Interactive chat mode"""
        coordinator = self.agents['coordinator']
        thread = self.threads['coordinator']
        
        print(f"\n🤖 APR Analysis System Ready!")
        print("💬 You're chatting with the APR Coordinator")
        print("📋 For APR analysis, just provide an APR number (e.g., '121' or 'analyze APR 121')")
        print("❓ For general questions, ask normally")
        print("🚪 Type 'exit' or 'quit' to end\n")
        
        while True:
            try:
                user_input = input("You: ").strip()
                if user_input.lower() in {"exit", "quit"}:
                    print("👋 Ending conversation...")
                    break
                    
                if not user_input:
                    continue
                
                # Check if user is requesting APR analysis
                # Simple detection - look for numbers that could be APR numbers
                import re
                apr_match = re.search(r'\b(\d{1,4})\b', user_input)
                
                if apr_match and any(keyword in user_input.lower() for keyword in 
                                   ['apr', 'analyze', 'analysis', 'report', 'metrics']):
                    # This looks like an APR analysis request
                    apr_number = apr_match.group(1)
                    print(f"\n🎯 Detected APR analysis request for APR {apr_number}")
                    
                    final_report = self.analyze_apr(apr_number)
                    print(f"\n📊 Final Report:\n")
                    print("=" * 80)
                    print(final_report)
                    print("=" * 80)
                    
                elif user_input.isdigit():
                    # Just a number - assume it's an APR number
                    apr_number = user_input
                    print(f"\n🎯 Running APR analysis for APR {apr_number}")
                    
                    final_report = self.analyze_apr(apr_number)
                    print(f"\n📊 Final Report:\n")
                    print("=" * 80)
                    print(final_report)
                    print("=" * 80)
                    
                else:
                    # Regular conversation - use coordinator
                    print("💭 Processing with coordinator...")
                    
                    self.agents_client.messages.create(
                        thread_id=thread.id,
                        role="user",
                        content=user_input
                    )
                    
                    run = self.agents_client.runs.create_and_process(
                        thread_id=thread.id,
                        agent_id=coordinator.id
                    )
                    
                    messages = list(self.agents_client.messages.list(thread_id=thread.id))
                    if messages and messages[0].role == MessageRole.AGENT:
                        response = messages[0].content[0].text.value
                        print(f"\n🤖 Coordinator: {response}\n")
                    else:
                        print("❌ Sorry, I didn't get a response. Please try again.\n")
                        
            except KeyboardInterrupt:
                print("\n👋 Goodbye!")
                break
            except Exception as e:
                print(f"❌ Error: {e}")
                print("Please try again.\n")
    
    def cleanup(self):
        """Delete all agents"""
        print("\n🧹 Cleaning up agents...")
        for agent_type, agent in self.agents.items():
            try:
                self.agents_client.delete_agent(agent.id)
                print(f"✅ Deleted {agent_type} agent")
            except Exception as e:
                print(f"⚠️ Error deleting {agent_type} agent: {e}")
        print("🎉 Cleanup completed")

# Main execution
if __name__ == "__main__":
    with project_client:
        agents_client = project_client.agents
        
        # Enable auto function calls
        agents_client.enable_auto_function_calls({
            get_jira_ticket_description, get_pull_request_body, get_pull_request_title,
            get_control_plan_metrics_from_pr_comment, get_jira_ticket_title, 
            get_jira_ticket_release_notes, get_jira_ticket_xlsx_attachment, 
            get_jira_ticket_attachments, get_PRs_from_apr, get_feature_rankings,
            get_pav_metrics_for_apr, get_ppa_metrics_for_apr, 
            get_sup_metrics_for_apr, get_dup_metrics_for_apr
        })
        
        model_deployment_name = os.getenv("MODEL_DEPLOYMENT_NAME")
        
        if model_deployment_name is not None:
            orchestrator = ManualOrchestration(agents_client, model_deployment_name)
            
            try:
                orchestrator.create_agents()
                orchestrator.chat_mode()
            finally:
                orchestrator.cleanup()
        else:
            print("❌ Error: Please define the environment variable MODEL_DEPLOYMENT_NAME.")