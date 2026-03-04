import json
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np

# Load the distilled data
with open('distilled_data.json', 'r') as f:
    data = json.load(f)

# Extract Metrics
sites = [item['url'].split('//')[-1].split('/')[0] for item in data]
confidences = [item['signals_data']['confidence_audit']['score'] for item in data]
integrity_status = [item['signals_data']['triggers']['is_high_integrity'] for item in data]

# 1. Pie Chart: Integrity
plt.figure(figsize=(6, 6))
plt.pie(pd.Series(integrity_status).value_counts(), labels=['High Integrity', 'Needs Audit'], 
        autopct='%1.1f%%', colors=['#4CAF50', '#F44336'])
plt.title('B2B Data Integrity Distribution')
plt.savefig('integrity_report.png')

# 2. Bar Chart: Confidence
plt.figure(figsize=(10, 5))
plt.bar(sites, confidences, color='skyblue')
plt.axhline(y=0.5, color='r', linestyle='--', label='Trust Threshold')
plt.title('Source-by-Source Confidence Audit')
plt.ylabel('Confidence Score')
plt.savefig('confidence_report.png')

print("✅ B2B Client Reports Generated: integrity_report.png, confidence_report.png")