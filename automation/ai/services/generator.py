from automation.ai.providers import get_provider

class AITestGenerator:
    def __init__(self, provider_name: str = "openai"):
        self.provider = get_provider(provider_name)
        
    def generate_test(self, requirements: str, framework: str = "pytest") -> str:
        prompt = f"Generate {framework} automation tests for the following requirements:\n{requirements}"
        system = "You are an expert SDET. Output only python code."
        return self.provider.generate(prompt, system_prompt=system)

class AIPageObjectGenerator:
    def __init__(self, provider_name: str = "openai"):
        self.provider = get_provider(provider_name)
        
    def generate_page_object(self, html_source: str, framework: str = "appium") -> str:
        prompt = f"Generate a {framework} Page Object Model for this source:\n{html_source[:2000]}"
        system = "You are an expert Automation Engineer. Output only python code."
        return self.provider.generate(prompt, system_prompt=system)
