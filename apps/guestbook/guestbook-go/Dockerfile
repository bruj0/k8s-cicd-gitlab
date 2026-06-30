FROM golang:1.23.2
WORKDIR /app
# Copy the Go modules files and download dependencies
COPY go.mod go.sum main.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -o guestbook-go .

FROM scratch
WORKDIR /app
COPY --from=0 /app/guestbook-go .
COPY ./public/index.html public/index.html
COPY ./public/script.js public/script.js
COPY ./public/style.css public/style.css
CMD ["/app/guestbook-go"]
EXPOSE 3000
