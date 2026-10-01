# Punjab District Attendance Dashboard

District-level Punjab attendance dashboard. No school-level rows are published.

## Data source

Punjab School Information System (SIS): https://sis.pesrp.edu.pk/dashboard

The collector uses the same SIS attendance endpoints already confirmed in the working Okara attendance collector, including teacher attendance, student attendance, Markaz/school discovery, and sanctioned-post staff totals.

## Run locally

```bash
python -m pip install requests
python punjab_district_collector.py
```

The live environment must have internet access to `sis.pesrp.edu.pk`.

## GitHub Actions

- Manual `workflow_dispatch`
- Daily 02:00 Asia/Karachi
- Collects Punjab district rows
- Validates that all discovered districts completed
- Commits `data/punjab_district_attendance.json`
- Deploys the dashboard to GitHub Pages

## Important

The collector is designed for GitHub Actions, where outbound HTTPS access is available. The ChatGPT runtime used to prepare this package cannot directly resolve the SIS host, so a full live collection could not be executed here. The script is syntax-checked; the first GitHub Actions run should be treated as the integration test.
