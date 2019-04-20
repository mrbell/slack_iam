A simple app to announce you're WFH or OOO via a slack command. 
Make it easy to notify your team when you'll be out of office.

Features:
* Set WFH or OOO for today, in the future, or in the past
* Whole day only, not partial days
* View your history
* View team status for the day
* View upcoming team status
* Take dates using a variety of human readable inputs

DB columns: user, date, status 

Statuses: 
- WFH - Working From Home
- OOO - Out of office on PTO or traveling or something, 
- INO - In office

Commands:

- /iam ooo [when (defaults to the current day)]  # Set yourself OOO
- /iam wfh [when (defaults to the current day)]  # Set yourself WFH
- /iam today  # Check everyone's status for the day
- /iam schedule  # Check upcoming scheduled status for the team
- /iam history  # Check your recent history