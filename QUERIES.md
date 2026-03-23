# Queries to the db

- Make a query :
```sql
sqlite3 strava.db
SELECT * FROM activities;
```

- Cumulative distance since timestamp : 
```sql
SELECT ROUND(SUM(distance) / 1000.0, 2) AS distance_km
FROM activities
WHERE sport_type = 'Ride'
  AND is_deleted = 0
  AND start_date >= '2026-01-01';
```

- Time series of cumulative distance :
```sql
SELECT
      a.start_date AS time,
      ROUND(SUM(b.distance) / 1000.0, 2) AS distance_km_cumul
  FROM activities a
  JOIN activities b
      ON b.start_date <= a.start_date
      AND b.sport_type = 'Ride'
      AND b.is_deleted = 0
      AND b.start_date >= '2026-01-01'
  WHERE a.sport_type = 'Ride'
    AND a.is_deleted = 0
    AND a.start_date >= '2026-01-01'
  GROUP BY a.id
  ORDER BY a.start_date
```

- Comparison of last year (B query) : 
```sql
SELECT
    '2026' || substr(a.start_date, 5) AS time,
    ROUND(SUM(b.distance) / 1000.0, 2) AS km_2025
FROM activities a
  JOIN activities b
      ON b.start_date <= a.start_date
      AND b.sport_type = 'Ride'
      AND b.is_deleted = 0
      AND b.start_date >= '2025-01-01'
      AND b.start_date < '2026-01-01'
  WHERE a.sport_type = 'Ride'
    AND a.is_deleted = 0
    AND a.start_date >= '2025-01-01'
    AND a.start_date < '2026-01-01'
  GROUP BY a.id
  ORDER BY a.start_date
```