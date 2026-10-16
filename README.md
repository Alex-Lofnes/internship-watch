# Internship watch

Checks the careers pages of every organisation on your tracker twice a day, emails you when a new internship, summer scheme or placement appears, and keeps a dashboard where you can tick off what you've applied to.

It runs for free on GitHub Actions, so nothing needs to stay switched on at your end.

## What it does on each run

1. For each organisation in `orgs.yaml`, it finds the careers page (or uses the one you've set) and reads every link on it.
2. Any link mentioning internships, summer schemes, placements, work experience, traineeships or insight days is recorded. If a link just says "Apply now", it uses the heading of the card the link sits in as the title.
3. If the careers page links to a standard recruitment system (Greenhouse, Lever, Workable, SmartRecruiters, Pinpoint), it reads that system's job list directly. The OECD is set up this way.
4. Pages that need JavaScript to show their listings are opened in a headless browser.
5. For schemes that announce openings on a fixed page (IFS summer students, the Fast Stream SIP, GSR placements, IfG, Chatham House and others), it also watches the wording about applications and deadlines. If "applications are closed" becomes "applications are now open", you get an email quoting the new wording.
6. It emails you anything new and updates the dashboard.

## Limitations

- **It finds links, not jobs.** Expect some noise, such as a link to a general "Internships" page or a blog post about last year's interns. Use Dismiss on the dashboard and they stay hidden.
- **Some sites won't work at first.** A few sites block automated visits, hide listings behind logins, or have careers pages the script can't find from the homepage. The Coverage tab lists every organisation with its status. Fixing one usually takes a minute: find the right careers page yourself and add it to `orgs.yaml` (see below).
- **Civil Service Jobs is skipped.** Its search needs a logged-in session. Use the job alerts built into Civil Service Jobs instead.
- **Ticks are saved in your browser.** They don't sync between your laptop and phone automatically. Use Export ticks and Import ticks to move them across.
- **The dashboard and your spreadsheet are separate.** Ticking something on the dashboard doesn't update the spreadsheet.

## Setup (about 20 minutes, once)

### 1. Get GitHub Pro free with the Student Developer Pack

Private repositories can only publish a website on GitHub Pro. As an LSE student you can get Pro free through the GitHub Student Developer Pack at https://education.github.com/pack. Verification can take a few days.

You could use a public repository instead and skip this step. The listings are all public anyway, and your ticks stay in your browser, but anyone with the link could see which organisations you're tracking.

### 2. Create the repository

1. On GitHub, create a new **private** repository called `internship-watch`.
2. Upload every file and folder from this zip, keeping the folder structure. The `.github` folder is hidden on some computers, so check it's there.

### 3. Create a Gmail app password

Your normal Gmail password won't work for sending from a script.

1. Turn on 2-Step Verification on your Google account if it isn't already.
2. Go to https://myaccount.google.com/apppasswords and create an app password called "Internship watch".
3. Copy the 16-character password.

### 4. Add your email settings to GitHub

In the repository, go to **Settings > Secrets and variables > Actions** and add these **secrets**:

| Name | Value |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USER` | your Gmail address |
| `SMTP_PASS` | the app password from step 3 |
| `EMAIL_TO` | where alerts should go (can be the same address) |

On the **Variables** tab of the same page, add `DASHBOARD_URL` once you know it (step 5). It's used to put a link to the dashboard in each email.

### 5. Turn on the dashboard

Go to **Settings > Pages**. Under "Build and deployment", set **Source** to **GitHub Actions**. Do this before the first run, or the publishing step will fail. Your dashboard address will be something like `https://yourname.github.io/internship-watch/`, and it appears on the Pages settings page after the first run.

### 6. Run it for the first time

Go to the **Actions** tab, choose **Check for internships**, and click **Run workflow**. The first run takes a few minutes. It emails you a short summary rather than every listing, since everything is new on the first run.

After that it runs automatically at 8am and 5pm UK time (7am and 4pm when the clocks change in March).

### 7. Fix coverage problems

Open the dashboard and go to the **Coverage** tab. Organisations with errors are listed first. For each one:

1. Find the organisation's real careers or vacancies page yourself.
2. Edit `orgs.yaml` on GitHub (click the file, then the pencil icon) and add a `careers:` line under that organisation:

```yaml
- name: Centre for Cities
  tier: '2'
  category: Think tank
  url: https://www.centreforcities.org
  careers:
  - https://www.centreforcities.org/about/jobs/
```

3. Commit the change. The next run will use it.

If a page is still empty after that, add `render: true` under the organisation so it's opened in a real browser.

## Everyday use

- **Emails** arrive only when something is new.
- **Listings tab:** new items since your last visit are highlighted. Tick the box once you've applied (it records the date). Dismiss anything irrelevant.
- **Page updates tab:** wording changes on watched scheme pages.
- **Coverage tab:** what was checked and what failed.

## Changing things

- **Add an organisation:** add an entry to `orgs.yaml` with at least `name` and `url`.
- **Stop checking one:** delete it, or add `skip: reason`.
- **Watch a scheme page for "applications open" wording:** add `watch_text: true` and list the page under `careers:`.
- **Change the schedule:** edit the `cron` lines in `.github/workflows/watch.yml`. The times are in UTC.
- **Change what counts as an internship:** edit `INTERN_RE` near the top of `watch.py`.

## Running it on your own computer

```bash
pip install -r requirements.txt
python -m playwright install chromium     # optional, for JavaScript-heavy pages

python watch.py --dry-run                 # check everything, print the email instead of sending it
python watch.py --only "Chatham" --dry-run  # check one organisation while fixing it
```

Open `docs/index.html` in your browser to see the dashboard.

## Being a polite visitor

The script identifies itself, follows each site's robots.txt rules, waits a second between requests, and only runs twice a day. Please keep it that way. Checking more often won't find much more, and it risks sites blocking you.
