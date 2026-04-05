import requests

class NetflixAccountVerifier:
    def __init__(self, email, password):
        self.email = email
        self.password = password

    def verify_account(self):
        # Implement verification logic with 100% accuracy.
        # It should return True if the account is valid and False otherwise.
        # This is a placeholder; actual implementation would depend on Netflix's API.
        response = requests.post('https://api.netflix.com/verify', data={'email': self.email, 'password': self.password})
        return response.json().get('valid', False)

class DataExtractor:
    def __init__(self, account_info):
        self.account_info = account_info

    def extract_data(self):
        # Enhanced data extraction logic goes here.
        # This could be logic to extract additional user data, view history, etc.
        return {
            'email': self.account_info['email'],
            'subscriptions': self.account_info['subscriptions'],
            'watch_history': self.account_info['watch_history'],
        }

class OutputFormatter:
    @staticmethod
    def format_output(data):
        # Improved formatting logic to match screenshot requirements.
        output = f"Account Email: {data['email']}\n" 
        output += f"Subscriptions: {', '.join(data['subscriptions'])}\n" 
        output += f"Watch History: {', '.join(data['watch_history'])}\n"
        return output

if __name__ == '__main__':
    # Example usage
    email = 'user@example.com'
    password = 'securepassword'
    verifier = NetflixAccountVerifier(email, password)
    if verifier.verify_account():
        account_info = {'email': email, 'subscriptions': ['Basic', 'Standard'], 'watch_history': ['Show A', 'Show B']}
        extractor = DataExtractor(account_info)
        extracted_data = extractor.extract_data()
        formatted_output = OutputFormatter.format_output(extracted_data)
        print(formatted_output)
    else:
        print('Invalid account.')