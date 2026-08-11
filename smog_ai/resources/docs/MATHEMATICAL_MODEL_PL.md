# Model matematyczny — skrót platformowy

Pełne źródło LaTeX jest dostępne w zakładce pobierania. Poniżej znajduje się skrót.

## Wektor celu

Dla stacji `s`, czasu bazowego `t` i horyzontu `h=1..48`:

```text
Y(s,t,h) = [PM10(s,t+h), PM2.5(s,t+h), T(s,t+h), R_Δ(s,t+h)]ᵀ
```

## Model temperatury

```text
T̂(s,t,h) = f_T(X_weather(s,t), h, φ(t+h), latitude, longitude, elevation)
```

## Model opadu typu hurdle

`R_Δ(s,t+h)` oznacza akumulację opadu w jawnym okresie `Δ` kończącym się
w czasie docelowym. Domyślnie `Δ=6 h`; nie jest to jednostka `mm/h` i system
nie dzieli pomiaru sześciogodzinnego przez sześć.

```text
π̂ = P(R_Δ > δ | X,h)
μ̂ = E[R_Δ | R_Δ > δ, X,h]
R̂_Δ = π̂ · μ̂
```

## Modele PM

```text
PM̂_k(s,t,h) = f_k(X_air, X_weather, h, φ(t+h), T̂, R̂, π̂, location)
```

Prognozowana pogoda użyta przez modele PM musi pochodzić z chronologicznego cross-fittingu, a nie z rzeczywistych przyszłych obserwacji.

## Interpolacja przestrzenna IDW

```text
w_i(x) = 1 / (d(x,x_i)+ε)^p
ŷ(x,u) = Σ w_i(x) ŷ_i(u) / Σ w_i(x)
```

## Czas niepełnogodzinowy

Dla pełnej godziny używana jest bezpośrednia prognoza modelu. Dla minut pomiędzy godzinami dopuszczalna jest lokalna interpolacja liniowa/PCHIP, bez ekstrapolacji.

## Ograniczona próba, wagi i szybkie douczanie

Niech pełny zbiór archiwalny będzie oznaczony przez \(\mathcal D\). Polityka
zbioru uczącego tworzy ograniczoną próbę

```text
D_train = S_policy(D; profile, budget),
```

gdzie profil określa okno czasu, maksymalną liczbę rekordów, liczbę horyzontów
na origin oraz liczbę foldów.

Waga obserwacji ma postać

```text
w_i = 2^{-a_i/τ} · n_station(i)^{-1/2} · n_horizon(i)^{-1/2},
```

gdzie `a_i` jest wiekiem w dniach, a `τ` okresem połowicznego zaniku. Po
normalizacji średnia waga wynosi 1.

Dla każdego originu losowany deterministycznie podzbiór horyzontów zachowuje
reprezentację koszyków `1–6`, `7–12`, `13–24`, `25–48`, dzięki czemu koszt
ekspansji jest ograniczony, a model nadal przyjmuje dokładne `horizon_hours`.

### Model resztowy online

Niech \(f_\theta(x,h)\) będzie kosztownym modelem bazowym. Po dojrzewaniu
prognozy obserwujemy

```text
r_i = y_i - f_θ(x_i,h_i).
```

Mały korektor jest aktualizowany przez `partial_fit`:

```text
g_{φ_{t+1}} ← Update(g_{φ_t}, z_i, r_i).
```

Prognoza końcowa:

```text
ŷ_i = clip(f_θ(x_i,h_i) + g_φ(z_i)).
```

Korektor jest promowany tylko wtedy, gdy na chronologicznej walidacji

```text
(MAE_base - MAE_corrected) / MAE_base ≥ η.
```

### Detekcja driftu

Dla okna referencyjnego i bieżącego definiujemy

```text
Δ_MAE = (MAE_recent - MAE_reference) / MAE_reference.
```

Pełny retraining jest rekomendowany, gdy

```text
Δ_MAE > δ_MAE
lub
|bias_recent| > δ_bias(target).
```

Budżet czasu jest elementem kontraktu treningu. Po jego wyczerpaniu nie
rozpoczynamy kolejnego kosztownego kandydata; aktywny model produkcyjny działa
bez przerwy do atomowej promocji nowej, zwalidowanej wersji.


## Generyczna rodzina celów jakości powietrza

Niech \(\mathcal P\) będzie konfigurowalnym zbiorem parametrów. Dla
\(p\in\mathcal P\), stacji \(s\), czasu bazowego \(t\) i horyzontu \(h\):

```text
Y^(p)_(s,t,h) = X^(p)_(s,t+h).
```

Wspólny kontrakt modelu ma postać

```text
ŷ^(p) = f_(p,θp)(x^(p)_(s,t), h, φ(t+h), w_(s,t+h), z^(p)_(s,t)),
```

gdzie `w` oznacza prognozowaną pogodę, a `z` zawiera wyłącznie dostępne w
chwili `t` wartości pomocniczych parametrów powietrza. Każdy parametr może mieć
własnego providera, jednostkę, przedział dopuszczalny i próg jakości.

Publikowana wartość jest projekcją na dziedzinę zdefiniowaną w rejestrze:

```text
ỹ^(p) = Π_(D_p)(ŷ^(p)).
```

Dodanie parametru do katalogu nie zwiększa kosztu ML. Koszt powstaje dopiero po
włączeniu roli `forecast_target`; selekcja `--targets` pozwala trenować pojedynczy
cel bez dezaktywowania pozostałych aktywnych modeli.
