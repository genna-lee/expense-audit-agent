import json
import sys

def main():
    try:
        with open('artifacts/grade_results/results_20260621_231003.json', 'r', encoding='utf-8') as f:
            res = json.load(f)
            
        for case in res.get('eval_case_results', []):
            if 'response_candidate_results' in case and case['response_candidate_results']:
                cand = case['response_candidate_results'][0]
                if 'metric_results' in cand:
                    for m in cand['metric_results']:
                        try:
                            score = m.get('metric_score')
                            if score is not None and score < 5:
                                print(f"Metric: {m.get('metric_name')}")
                                print(f"Score: {score}")
                                print(f"Explanation: {m.get('explanation')}")
                                print("-" * 40)
                        except Exception as e:
                            print(f"Error parsing metric: {m}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    main()
